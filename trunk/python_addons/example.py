#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SRS Python Addon Example
=========================

This file demonstrates the correct way to write Python addons for SRS that work
properly in both console mode (daemon=off) and file mode (daemon=on).

Key Concepts:
-------------
1. Logger Initialization: Use get_logger() to initialize SRS logger
2. Signal Handling: Different strategies for console vs file mode
3. Process Lifecycle: Let SRS manage child process termination

Usage:
------
Console Mode (Development):
    - Set in srs.conf: daemon off; srs_log_tank console;
    - Logs output to terminal with colors
    - SRS uses SIGKILL to terminate addon (no graceful shutdown)
    - DO NOT install signal handlers for SIGINT/SIGTERM
    - Ctrl+C stops SRS and all addons

File Mode (Production):
    - Set in srs.conf: daemon on; srs_log_tank file;
    - Logs output to srs_log_file
    - SRS uses SIGTERM first, then SIGKILL
    - CAN install SIGTERM handler for graceful shutdown
    - Process runs as daemon in background

Author: NPL ITP Team - Infrastructre Group - Jason-JP-Yang - Intra-Stream Authors
License: MIT
"""

import sys
import argparse
import time
import signal
import threading
from typing import Optional

# Import the SRS logger module
from srs_logger import get_logger

# ============================================================================
# Global State
# ============================================================================

# Flag to signal the main loop to exit
shutdown_requested = False

# ============================================================================
# Signal Handling
# ============================================================================

def setup_signal_handlers(log_tank: str, logger) -> None:
    """
    Configure signal handlers based on log mode.
    
    Args:
        log_tank: Log mode ('console' or 'file')
        logger: SRS logger instance
    
    Signal Handling Rules:
    ----------------------
    CONSOLE MODE (log_tank='console', daemon=off):
    - C++ fork() sets signal(SIGINT, SIG_IGN) and signal(SIGTERM, SIG_IGN)
    - Python MUST NOT override these with signal.signal()
    - SRS parent process handles Ctrl+C and sends SIGKILL to children
    - No graceful shutdown possible in console mode
    
    FILE MODE (log_tank='file', daemon=on):
    - SRS runs as daemon, detached from terminal
    - Python can install SIGTERM handler for graceful shutdown
    - SRS sends SIGTERM first, waits, then sends SIGKILL if needed
    """
    global shutdown_requested
    
    if log_tank == 'file':
        # In file mode, we can handle SIGTERM for graceful shutdown
        def handle_sigterm(signum, frame):
            global shutdown_requested
            try:
                signame = signal.Signals(signum).name
            except Exception:
                signame = str(signum)
            
            logger.info(f"Received signal {signame}, initiating graceful shutdown...")
            shutdown_requested = True
        
        try:
            signal.signal(signal.SIGTERM, handle_sigterm)
            logger.info("Registered SIGTERM handler for graceful shutdown (file mode)")
        except Exception as e:
            logger.warn(f"Failed to register SIGTERM handler: {e}")
    else:
        # In console mode, do NOT install any signal handlers
        # The C++ code has already set SIG_IGN, and we must respect that
        logger.info("Console mode: signal handling managed by SRS parent process")
        logger.info("Process will be terminated with SIGKILL (no graceful shutdown)")

# ============================================================================
# Example Worker Functions
# ============================================================================

def simple_worker(logger, duration: int = 30):
    """
    Simple worker that runs for a specified duration.
    
    Args:
        logger: SRS logger instance
        duration: How long to run (seconds)
    """
    logger.info(f"Simple worker started, will run for {duration} seconds")
    
    start_time = time.time()
    iteration = 0
    
    while not shutdown_requested:
        elapsed = time.time() - start_time
        if elapsed >= duration:
            logger.info(f"Worker completed after {duration} seconds")
            break
        
        iteration += 1
        logger.debug(f"Worker iteration {iteration}, elapsed: {elapsed:.1f}s")
        time.sleep(1)
    
    if shutdown_requested:
        logger.info("Worker stopped due to shutdown request")

def analytics_worker(logger, interval: int = 5):
    """
    Analytics worker that periodically processes data.
    
    Args:
        logger: SRS logger instance
        interval: Processing interval (seconds)
    """
    logger.info(f"Analytics worker started, processing every {interval} seconds")
    
    batch_number = 0
    
    while not shutdown_requested:
        try:
            batch_number += 1
            logger.info(f"Processing analytics batch #{batch_number}")
            
            # Simulate data processing
            for i in range(5):
                if shutdown_requested:
                    break
                logger.debug(f"  - Processing chunk {i+1}/5")
                time.sleep(interval / 5)
            
            logger.info(f"Analytics batch #{batch_number} completed")
            
            # Wait for next interval
            for _ in range(interval):
                if shutdown_requested:
                    break
                time.sleep(1)
                
        except Exception as e:
            logger.exception(f"Error in analytics worker: {e}")
            # Continue processing despite errors
    
    logger.info("Analytics worker stopped")

def api_server_worker(logger, port: int = 8888):
    """
    Simulated API server worker.
    
    This demonstrates a long-running service that needs graceful shutdown.
    In a real implementation, this would be an HTTP server, WebSocket server,
    or other network service.
    
    Args:
        logger: SRS logger instance
        port: Port number to simulate binding
    """
    logger.info(f"API server worker started on port {port}")
    
    # Simulate server initialization
    logger.info(f"Binding to 0.0.0.0:{port}")
    time.sleep(0.5)
    logger.info("Server ready to accept connections")
    
    request_count = 0
    
    # Main server loop
    while not shutdown_requested:
        # Simulate handling requests
        time.sleep(2)
        request_count += 1
        logger.debug(f"Handled request #{request_count}")
    
    # Graceful shutdown
    logger.info("Closing server connections...")
    time.sleep(0.5)
    logger.info(f"API server stopped after handling {request_count} requests")

# ============================================================================
# Main Entry Point
# ============================================================================

def main():
    """
    Main function demonstrating proper Python addon structure.
    """
    parser = argparse.ArgumentParser(
        description="SRS Python Addon Example",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Run simple worker for 30 seconds
  python3 example.py --worker simple --duration 30
  
  # Run analytics worker with 10 second interval
  python3 example.py --worker analytics --interval 10
  
  # Run API server on port 9000
  python3 example.py --worker api-server --port 9000
  
  # Use custom config file
  python3 example.py --config /path/to/srs.conf --worker simple
        """
    )
    
    parser.add_argument(
        "--config",
        default="./conf/srs.conf",
        help="Path to SRS config file (default: ./conf/srs.conf)"
    )
    
    parser.add_argument(
        "--worker",
        choices=['simple', 'analytics', 'api-server'],
        default='simple',
        help="Type of worker to run (default: simple)"
    )
    
    parser.add_argument(
        "--duration",
        type=int,
        default=30,
        help="Duration for simple worker in seconds (default: 30)"
    )
    
    parser.add_argument(
        "--interval",
        type=int,
        default=5,
        help="Interval for analytics worker in seconds (default: 5)"
    )
    
    parser.add_argument(
        "--port",
        type=int,
        default=8888,
        help="Port for API server worker (default: 8888)"
    )
    
    args = parser.parse_args()
    
    try:
        # ====================================================================
        # Step 1: Initialize SRS Logger
        # ====================================================================
        # Always initialize logger FIRST before any other operations
        logger = get_logger(args.config)
        
        # Get log configuration to determine signal handling strategy
        log_tank = (logger.config.get('log_tank') or 'console').lower()
        log_file = logger.config.get('log_file', './objs/srs.log')
        
        logger.info("=" * 70)
        logger.info("SRS Python Addon Example Starting")
        logger.info("=" * 70)
        logger.info(f"Config file: {args.config}")
        logger.info(f"Log mode: {log_tank}")
        logger.info(f"Log file: {log_file}")
        logger.info(f"Worker type: {args.worker}")
        logger.info(f"Process PID: {threading.current_thread().ident}")
        
        # ====================================================================
        # Step 2: Setup Signal Handlers
        # ====================================================================
        # Configure signal handling based on log mode
        setup_signal_handlers(log_tank, logger)
        
        # ====================================================================
        # Step 3: Run Worker
        # ====================================================================
        # Execute the selected worker function
        logger.info("Starting worker...")
        
        if args.worker == 'simple':
            simple_worker(logger, args.duration)
        elif args.worker == 'analytics':
            analytics_worker(logger, args.interval)
        elif args.worker == 'api-server':
            api_server_worker(logger, args.port)
        
        # ====================================================================
        # Step 4: Cleanup
        # ====================================================================
        logger.info("Worker completed successfully")
        logger.info("=" * 70)
        logger.info("SRS Python Addon Example Finished")
        logger.info("=" * 70)
        
        return 0
        
    except KeyboardInterrupt:
        # This should only happen in console mode if SRS doesn't catch it first
        if 'logger' in locals():
            logger.warn("Received KeyboardInterrupt")
        return 130  # Standard exit code for SIGINT
        
    except Exception as e:
        # Log any unhandled exceptions
        if 'logger' in locals():
            logger.exception(f"Unhandled exception in main: {e}")
        else:
            print(f"FATAL ERROR: {e}", file=sys.stderr)
            import traceback
            traceback.print_exc()
        
        return 1

if __name__ == "__main__":
    sys.exit(main())
