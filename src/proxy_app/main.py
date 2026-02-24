# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Mirrowel

"""
LLM API Key Proxy - Main entry point.

This module handles:
- CLI argument parsing
- TUI launcher mode
- Credential tool mode
- Application startup

The actual FastAPI application is created via app_factory.create_app().
"""

import time
import os
from pathlib import Path
import sys
import argparse
import logging

# --- Argument Parsing (BEFORE heavy imports) ---
parser = argparse.ArgumentParser(description="API Key Proxy Server")
parser.add_argument(
    "--host", type=str, default="0.0.0.0", help="Host to bind the server to."
)
parser.add_argument("--port", type=int, default=8000, help="Port to run the server on.")
parser.add_argument(
    "--enable-request-logging",
    action="store_true",
    help="Enable transaction logging in the library (logs request/response with provider correlation).",
)
parser.add_argument(
    "--enable-raw-logging",
    action="store_true",
    help="Enable raw I/O logging at proxy boundary (captures unmodified HTTP data, disabled by default).",
)
parser.add_argument(
    "--add-credential",
    action="store_true",
    help="Launch the interactive tool to add a new OAuth credential.",
)
args, _ = parser.parse_known_args()

# Add the 'src' directory to the Python path
sys.path.append(str(Path(__file__).resolve().parent.parent))

# Check if we should launch TUI (no arguments = TUI mode)
if len(sys.argv) == 1:
    # TUI MODE - Load ONLY what's needed for the launcher (fast path!)
    from proxy_app.launcher_tui import run_launcher_tui

    run_launcher_tui()
    # Re-parse arguments with modified sys.argv
    args = parser.parse_args()

# Check if credential tool mode
if args.add_credential:
    from rotator_library.credential_tool import run_credential_tool

    run_credential_tool()
    sys.exit(0)

# If we get here, we're ACTUALLY running the proxy
_start_time = time.time()

# Load environment variables
from dotenv import load_dotenv

if getattr(sys, "frozen", False):
    _root_dir = Path(sys.executable).parent
else:
    _root_dir = Path.cwd()

load_dotenv(_root_dir / ".env")

# Load additional .env files
_env_files_found = list(_root_dir.glob("*.env"))
for _env_file in sorted(_root_dir.glob("*.env")):
    if _env_file.name != ".env":
        load_dotenv(_env_file, override=False)

if _env_files_found:
    _env_names = [_ef.name for _ef in _env_files_found]
    print(f"📁 Loaded {len(_env_files_found)} .env file(s): {', '.join(_env_names)}")

# Get proxy API key for display
proxy_api_key = os.getenv("PROXY_API_KEY")


def _mask_api_key(key: str) -> str:
    """Mask API key for safe display in logs. Shows first 4 and last 4 chars."""
    if not key or len(key) <= 8:
        return "****"
    return f"{key[:4]}****{key[-4:]}"


if proxy_api_key:
    key_display = f"✓ {_mask_api_key(proxy_api_key)}"
else:
    key_display = "✗ Not Set (INSECURE - anyone can access!)"

print("━" * 70)
print(f"Starting proxy on {args.host}:{args.port}")
print(f"Proxy API Key: {key_display}")
print(f"GitHub: https://github.com/Mirrowel/LLM-API-Key-Proxy")
print("━" * 70)
print("Loading server components...")

# Phase 2: Load Rich for loading spinner
from rich.console import Console

_console = Console()

# Phase 3: Heavy dependencies with granular loading messages
print("  → Loading FastAPI framework...")
with _console.status("[dim]Loading FastAPI framework...", spinner="dots"):
    import litellm

print("  → Loading core dependencies...")
with _console.status("[dim]Loading core dependencies...", spinner="dots"):
    from rotator_library.utils.paths import get_logs_dir, get_data_file

print("  → Initializing proxy core...")
with _console.status("[dim]Initializing proxy core...", spinner="dots"):
    from proxy_app.app_factory import create_app
    from proxy_app.startup import _discover_api_keys

# Calculate loading time
_elapsed = time.time() - _start_time
print(f"✓ Server ready in {_elapsed:.2f}s")

# Clear screen and reprint header
import os as _os_module
_os_module.system("cls" if _os_module.name == "nt" else "clear")

print("━" * 70)
print(f"Starting proxy on {args.host}:{args.port}")
print(f"Proxy API Key: {key_display}")
print(f"GitHub: https://github.com/Mirrowel/LLM-API-Key-Proxy")
print("━" * 70)
print(f"✓ Server ready in {_elapsed:.2f}s")

# --- Logging Configuration ---
LOG_DIR = get_logs_dir(_root_dir)

# Configure logging
import colorlog

console_handler = colorlog.StreamHandler(sys.stdout)
console_handler.setLevel(logging.INFO)
formatter = colorlog.ColoredFormatter(
    "%(log_color)s%(message)s",
    log_colors={
        "DEBUG": "cyan",
        "INFO": "green",
        "WARNING": "yellow",
        "ERROR": "red",
        "CRITICAL": "red,bg_white",
    },
)
console_handler.setFormatter(formatter)

# File handlers
info_file_handler = logging.FileHandler(LOG_DIR / "proxy.log", encoding="utf-8")
info_file_handler.setLevel(logging.INFO)
info_file_handler.setFormatter(
    logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
)

debug_file_handler = logging.FileHandler(LOG_DIR / "proxy_debug.log", encoding="utf-8")
debug_file_handler.setLevel(logging.DEBUG)
debug_file_handler.setFormatter(
    logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
)


class RotatorDebugFilter(logging.Filter):
    def filter(self, record):
        return record.levelno == logging.DEBUG and record.name.startswith("rotator_library")


debug_file_handler.addFilter(RotatorDebugFilter())


class NoLiteLLMLogFilter(logging.Filter):
    def filter(self, record):
        return not record.name.startswith("LiteLLM")


console_handler.addFilter(NoLiteLLMLogFilter())

# Configure root logger
root_logger = logging.getLogger()
root_logger.setLevel(logging.DEBUG)
root_logger.addHandler(info_file_handler)
root_logger.addHandler(console_handler)
root_logger.addHandler(debug_file_handler)

# Silence noisy loggers
logging.getLogger("uvicorn").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("LiteLLM").setLevel(logging.WARNING)

# Isolate LiteLLM's logger
litellm_logger = logging.getLogger("LiteLLM")
litellm_logger.handlers = []
litellm_logger.propagate = False

# Set environment flags from args
if args.enable_request_logging:
    os.environ["ENABLE_REQUEST_LOGGING"] = "true"
    logging.info("Transaction logging is enabled.")

if args.enable_raw_logging:
    os.environ["ENABLE_RAW_LOGGING"] = "true"
    logging.info("Raw I/O logging is enabled.")

# Create the FastAPI application
app = create_app(data_dir=_root_dir)

if __name__ == "__main__":
    import uvicorn

    # Check for onboarding
    ENV_FILE = get_data_file(".env")

    def needs_onboarding() -> bool:
        """Check if the proxy needs onboarding."""
        return not ENV_FILE.is_file()

    def show_onboarding_message():
        """Display onboarding message."""
        from rich.panel import Panel

        _console.print(
            Panel.fit(
                "[bold cyan]🚀 LLM API Key Proxy - First Time Setup[/bold cyan]",
                border_style="cyan",
            )
        )
        _console.print("[bold yellow]:warning:  Configuration Required[/bold yellow]\n")
        _console.print("The proxy needs initial configuration:")
        _console.print("  [red]:x: No .env file found[/red]")
        _console.print("\n[bold]What happens next:[/bold]")
        _console.print("  1. We'll create a .env file with PROXY_API_KEY")
        _console.print("  2. You can add LLM provider credentials")
        _console.print("  3. The proxy will then start normally")
        _console.input(
            "\n[bold green]Press Enter to launch the credential setup tool...[/bold green]"
        )

    # Check onboarding
    if needs_onboarding():
        show_onboarding_message()
        from rotator_library.credential_tool import ensure_env_defaults, run_credential_tool

        ensure_env_defaults()
        load_dotenv(ENV_FILE, override=True)
        run_credential_tool()
        load_dotenv(ENV_FILE, override=True)

        if needs_onboarding():
            _console.print("\n[bold red]:x: Configuration incomplete.[/bold red]")
            sys.exit(1)
        else:
            _console.print("\n[bold green]:white_check_mark: Configuration complete![/bold green]")

    uvicorn.run(app, host=args.host, port=args.port)
