//
// Copyright (c) 2013-2025 The SRS Authors
//
// SPDX-License-Identifier: MIT
//

#ifndef SRS_APP_PYTHON_ADDONS_HPP
#define SRS_APP_PYTHON_ADDONS_HPP

#include <srs_core.hpp>

#include <string>
#include <vector>

#include <srs_app_st.hpp>

class SrsProcess;
class ISrsCoroutine;
class ISrsAppConfig;

// Manage external Python addon processes configured via python_addons directive.
class SrsPythonAddons : public ISrsCoroutineHandler
{
private:
    struct SrsPythonAddonEntry {
        std::string script_path;
        std::string work_dir;
        std::vector<std::string> args;
        std::string summary;
        SrsProcess *process;
        uint32_t restart_attempts;
        srs_utime_t next_start_at;
        bool disabled;

        SrsPythonAddonEntry();
    };

private:
    ISrsAppConfig *config_;
    ISrsCoroutine *trd_;
    bool running_;
    std::string base_dir_;
    std::string python_bin_;
    std::vector<SrsPythonAddonEntry> addons_;
    srs_utime_t tick_interval_;
    int restart_limit_;
    bool restart_always_;
    srs_utime_t retry_interval_;
    bool failure_exit_;

public:
    SrsPythonAddons();
    virtual ~SrsPythonAddons();

public:
    // Load configuration and start background manager. Safe to call multiple times.
    virtual srs_error_t start();
    // Stop all managed python processes. When fast=true, use SIGKILL as fallback immediately.
    virtual void shutdown(bool fast);

public:
    // ISrsCoroutineHandler
    virtual srs_error_t cycle();

private:
    virtual srs_error_t reload_from_config();
    virtual void clear_addons();
    virtual void stop_processes(bool fast);
    static std::vector<std::string> split_args(const std::string &line);
    static std::string resolve_path(const std::string &base, const std::string &path);
    static std::vector<std::string> build_configure_args();
    static std::string join_args(const std::vector<std::string> &args);
};

#endif
