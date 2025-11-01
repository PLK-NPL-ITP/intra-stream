//
// Copyright (c) 2013-2025 The SRS Authors
//
// SPDX-License-Identifier: MIT
//

#include <srs_app_python_addons.hpp>

#include <algorithm>
#include <cctype>
#include <sstream>

#include <signal.h>
#if !defined(_WIN32)
#include <unistd.h>
#endif

#include <srs_app_config.hpp>
#include <srs_app_process.hpp>
#include <srs_kernel_error.hpp>
#include <srs_kernel_log.hpp>
#include <srs_kernel_utility.hpp>

SrsPythonAddons::SrsPythonAddonEntry::SrsPythonAddonEntry()
{
    process = NULL;
    restart_attempts = 0;
    next_start_at = 0;
    disabled = false;
}

SrsPythonAddons::SrsPythonAddons()
{
    config_ = _srs_config;
    trd_ = new SrsDummyCoroutine();
    running_ = false;
    tick_interval_ = 1 * SRS_UTIME_SECONDS;
    restart_limit_ = 3;
    restart_always_ = false;
    retry_interval_ = 5 * SRS_UTIME_SECONDS;
    failure_exit_ = true;
}

SrsPythonAddons::~SrsPythonAddons()
{
    shutdown(true);
    srs_freep(trd_);
    clear_addons();
    config_ = NULL;
}

srs_error_t SrsPythonAddons::start()
{
    srs_error_t err = srs_success;

    // Reload configuration for every start to pick up latest settings.
    if ((err = reload_from_config()) != srs_success) {
        clear_addons();
        return srs_error_wrap(err, "reload python addons config");
    }

    if (addons_.empty()) {
        // Either disabled or no addons defined.
        return err;
    }

    if (running_) {
        return err;
    }

    srs_freep(trd_);
    trd_ = new SrsSTCoroutine("python-addons", this);
    if ((err = trd_->start()) != srs_success) {
        clear_addons();
        return srs_error_wrap(err, "start python addons coroutine");
    }

    running_ = true;
    srs_trace("python_addons: started %d addon(s)", (int)addons_.size());

    return err;
}

void SrsPythonAddons::shutdown(bool fast)
{
    if (running_) {
        trd_->stop();
        running_ = false;
    }

    stop_processes(fast);

    clear_addons();

    srs_freep(trd_);
    trd_ = new SrsDummyCoroutine();
}

srs_error_t SrsPythonAddons::cycle()
{
    srs_error_t err = srs_success;

    while (true) {
        if ((err = trd_->pull()) != srs_success) {
            err = srs_error_wrap(err, "python addons cycle");
            break;
        }

        srs_utime_t now = srs_time_now_cached();

        for (std::vector<SrsPythonAddonEntry>::iterator it = addons_.begin(); it != addons_.end(); ++it) {
            SrsPythonAddonEntry &addon = *it;

            if (addon.disabled || !addon.process) {
                continue;
            }

            if (!addon.process->started()) {
                if (addon.next_start_at > now) {
                    continue;
                }

                // If this is a scheduled retry (not the very first start), log a succinct restart message.
                if (addon.restart_attempts > 0 || addon.next_start_at != 0) {
                    srs_warn("python_addons: restarting addon %s now (attempt=%u)", addon.script_path.c_str(), addon.restart_attempts);
                }

                if ((err = addon.process->start()) != srs_success) {
                    return srs_error_wrap(err, "start python addon %s", addon.script_path.c_str());
                }
            }

            bool was_running = addon.process->started();

            if (was_running) {
                if ((err = addon.process->cycle()) != srs_success) {
                    return srs_error_wrap(err, "cycle python addon %s", addon.script_path.c_str());
                }
            }

            if (was_running && !addon.process->started()) {
                // A run just terminated. Decide whether to schedule a restart.
                addon.restart_attempts += 1;

                // If there's a limit and we've reached it, don't schedule another restart.
                if (!restart_always_ && restart_limit_ >= 0 && (int)addon.restart_attempts > restart_limit_) {
                    srs_error("python_addons: addon %s exceeded restart limit %d", addon.script_path.c_str(), restart_limit_);
                    if (failure_exit_) {
                        srs_error("python_addons: terminating SRS due to addon restart exhaustion");
                        // Prefer SIGTERM over SIGKILL.
                        ::raise(SIGTERM);
                        return srs_error_new(ERROR_PYTHON_ADDONS_CONFIG, "python addon restart limit reached");
                    }
                    addon.disabled = true;
                    addon.next_start_at = 0;
                    continue;
                }

                // Schedule next restart after retry_interval_ and log summary of attempt and delay.
                addon.next_start_at = srs_time_now_cached() + retry_interval_;
                double retry_seconds = (double)retry_interval_ / SRS_UTIME_SECONDS;
                if (restart_always_ || restart_limit_ < 0) {
                    srs_warn("python_addons: addon %s exited, will retry in %.02fs (attempt=%u)", addon.script_path.c_str(), retry_seconds, addon.restart_attempts);
                } else {
                    srs_warn("python_addons: addon %s exited, will retry in %.02fs (attempt=%u/%d)", addon.script_path.c_str(), retry_seconds, addon.restart_attempts, restart_limit_);
                }
                continue;
            }

            if (addon.process->started()) {
                addon.next_start_at = 0;
            }
        }

        srs_usleep(tick_interval_);
    }

    return err;
}

srs_error_t SrsPythonAddons::reload_from_config()
{
    srs_error_t err = srs_success;

    clear_addons();

    if (!config_->get_python_addons_enabled()) {
        srs_trace("python_addons: disabled");
        return err;
    }

    base_dir_ = config_->cwd();
    if (base_dir_.empty()) {
        base_dir_ = ".";
    }

    python_bin_ = resolve_path(base_dir_, "objs/python_venv/bin/python");

    SrsPath path;
    if (!path.exists(python_bin_)) {
        return srs_error_new(ERROR_PYTHON_ADDONS_CONFIG, "python_addons: interpreter %s not found", python_bin_.c_str());
    }

    std::vector<SrsConfDirective *> nodes = config_->get_python_addons();
    if (nodes.empty()) {
        srs_warn("python_addons: enabled but no addon configured");
        return err;
    }

    int restart_conf = config_->get_python_addons_restart();
    restart_limit_ = restart_conf;
    restart_always_ = (restart_conf < 0);
    retry_interval_ = config_->get_python_addons_retry_interval();
    if (retry_interval_ < 0) {
        retry_interval_ = 0;
    }
    failure_exit_ = config_->get_python_addons_failure_exit();

    srs_info("python_addons: restart=%s, retry_interval=%.02f s, failure_exit=%s",
             (restart_always_ ? "always" : srs_strconv_format_int(restart_limit_).c_str()),
             (double)retry_interval_ / SRS_UTIME_SECONDS,
             failure_exit_ ? "on" : "off");

    std::vector<std::string> configure_args = build_configure_args();

    for (size_t i = 0; i < nodes.size(); ++i) {
        SrsConfDirective *node = nodes[i];

        std::string script = config_->get_python_addon_script(node);
        if (script.empty()) {
            return srs_error_new(ERROR_PYTHON_ADDONS_CONFIG, "python_addons: addon #%d missing script", (int)i);
        }

        std::string script_path = resolve_path(base_dir_, script);
        if (!path.exists(script_path)) {
            return srs_error_new(ERROR_PYTHON_ADDONS_CONFIG, "python_addons: script %s not found", script_path.c_str());
        }

        std::string work_dir = config_->get_python_addon_work_dir(node);
        if (work_dir.empty()) {
            work_dir = base_dir_;
        } else {
            work_dir = resolve_path(base_dir_, work_dir);
        }

    std::string args_line = config_->get_python_addon_args(node);
    std::vector<std::string> args = split_args(args_line);
    std::vector<std::string> combined_args = args;
    // Always append the configure summary so every addon sees the entire configure context.
    combined_args.insert(combined_args.end(), configure_args.begin(), configure_args.end());

    std::vector<std::string> argv;
    argv.push_back(python_bin_);
    argv.push_back(script_path);
    argv.insert(argv.end(), combined_args.begin(), combined_args.end());

        SrsProcess *process = new SrsProcess();
        process->set_work_dir(work_dir);
#ifdef __linux__
        process->set_parent_exit_signal(SIGTERM);
#endif
        if ((err = process->initialize(python_bin_, argv)) != srs_success) {
            srs_freep(process);
            return srs_error_wrap(err, "init python addon %s", script_path.c_str());
        }

        std::string combined_args_line = join_args(combined_args);
        if (combined_args_line.empty()) {
            srs_info("python_addons: addon #%d script=%s, work_dir=%s", (int)i, script_path.c_str(), work_dir.c_str());
        } else {
            srs_info("python_addons: addon #%d script=%s, work_dir=%s, args=%s", (int)i, script_path.c_str(), work_dir.c_str(), combined_args_line.c_str());
        }

        SrsPythonAddonEntry addon;
        addon.script_path = script_path;
        addon.work_dir = work_dir;
        addon.args = combined_args;
        if (combined_args_line.empty()) {
            addon.summary = script_path;
        } else {
            addon.summary = srs_fmt_sprintf("%s %s", script_path.c_str(), combined_args_line.c_str());
        }
        addon.process = process;
        addons_.push_back(addon);
    }

    srs_trace("python_addons: prepared %d addon(s)", (int)addons_.size());

    return err;
}

void SrsPythonAddons::clear_addons()
{
    for (size_t i = 0; i < addons_.size(); ++i) {
        srs_freep(addons_[i].process);
    }
    addons_.clear();
}

void SrsPythonAddons::stop_processes(bool fast)
{
    if (addons_.empty()) {
        return;
    }

    if (fast) {
        for (size_t i = 0; i < addons_.size(); ++i) {
            if (addons_[i].process) {
                addons_[i].process->fast_kill();
            }
        }
        return;
    }

    for (size_t i = 0; i < addons_.size(); ++i) {
        if (addons_[i].process) {
            addons_[i].process->fast_stop();
        }
    }

    srs_usleep(100 * SRS_UTIME_MILLISECONDS);

    for (size_t i = 0; i < addons_.size(); ++i) {
        if (addons_[i].process) {
            addons_[i].process->stop();
        }
    }
}

std::vector<std::string> SrsPythonAddons::build_configure_args()
{
    std::vector<std::string> out;

    std::string configure_line;
#ifdef SRS_CONFIGURE
    configure_line = SRS_CONFIGURE;
#endif

    if (configure_line.empty()) {
        return out;
    }

    std::vector<std::string> tokens = split_args(configure_line);
    out.reserve(tokens.size());

    for (size_t i = 0; i < tokens.size(); ++i) {
        std::string token = tokens.at(i);
        if (token.empty()) {
            continue;
        }

        std::string suffix = token;
        if (suffix.rfind("--", 0) == 0) {
            suffix.erase(0, 2);
        } else if (!suffix.empty() && suffix.at(0) == '-') {
            suffix.erase(0, 1);
        }

        if (suffix.empty()) {
            suffix = token;
        }

        out.push_back(std::string("--configure-") + suffix);
    }

    return out;
}

std::string SrsPythonAddons::join_args(const std::vector<std::string> &args)
{
    if (args.empty()) {
        return std::string();
    }

    std::ostringstream ss;
    for (size_t i = 0; i < args.size(); ++i) {
        if (i > 0) {
            ss << ' ';
        }
        ss << args.at(i);
    }
    return ss.str();
}

std::vector<std::string> SrsPythonAddons::split_args(const std::string &line)
{
    std::vector<std::string> out;
    std::string current;
    bool in_single = false;
    bool in_double = false;
    bool escape = false;

    for (size_t i = 0; i < line.size(); ++i) {
        char c = line.at(i);

        if (escape) {
            current.push_back(c);
            escape = false;
            continue;
        }

        if (c == '\\') {
            escape = true;
            continue;
        }

        if (c == '"' && !in_single) {
            in_double = !in_double;
            continue;
        }

        if (c == '\'' && !in_double) {
            in_single = !in_single;
            continue;
        }

        if (!in_single && !in_double && std::isspace(static_cast<unsigned char>(c))) {
            if (!current.empty()) {
                out.push_back(current);
                current.clear();
            }
            continue;
        }

        current.push_back(c);
    }

    if (!current.empty()) {
        out.push_back(current);
    }

    return out;
}

std::string SrsPythonAddons::resolve_path(const std::string &base, const std::string &path)
{
    if (path.empty()) {
        return base;
    }

    if (srs_strings_starts_with(path, "/")) {
        return path;
    }

#if defined(_WIN32) || defined(_WIN64)
    if (path.size() > 1 && path[1] == ':') {
        return path;
    }
#endif

    std::string combined = base;
    if (!combined.empty() && combined.at(combined.size() - 1) != '/') {
        combined.append("/");
    }
    combined.append(path);

    bool absolute = !combined.empty() && combined.at(0) == '/';
    std::vector<std::string> parts = srs_strings_split(combined, "/");
    std::vector<std::string> normalized;
    for (size_t i = 0; i < parts.size(); ++i) {
        const std::string &token = parts.at(i);
        if (token.empty() || token == ".") {
            continue;
        }
        if (token == "..") {
            if (!normalized.empty() && normalized.back() != "..") {
                normalized.pop_back();
            } else if (!absolute) {
                normalized.push_back(token);
            }
            continue;
        }
        normalized.push_back(token);
    }

    std::stringstream ss;
    if (absolute) {
        ss << '/';
    }

    for (size_t i = 0; i < normalized.size(); ++i) {
        ss << normalized.at(i);
        if (i + 1 < normalized.size()) {
            ss << '/';
        }
    }

    std::string resolved = ss.str();
    if (resolved.empty()) {
        return absolute ? std::string("/") : std::string(".");
    }
    return resolved;
}
