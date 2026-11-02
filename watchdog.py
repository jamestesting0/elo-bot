# watchdog.py
import atexit
import subprocess
import signal
import time
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass(frozen=True)
class WatchdogConfig:
    bot_cmd: list[str]
    check_every_sec: int = 30
    down_threshold_sec: int = 600
    stop_timeout_sec: int = 5


class BotWatchdog:
    def __init__(self, cfg: WatchdogConfig):
        self.cfg = cfg
        self.child: Optional[subprocess.Popen] = None
        self.down_since: Optional[float] = None

    def start_bot(self):
        print("[watchdog] starting bot:", " ".join(self.cfg.bot_cmd), flush=True)
        self.child = subprocess.Popen(self.cfg.bot_cmd)
        self.down_since = None

    def stop_bot(self):
        if self.child is None:
            return

        try:
            if self.child.poll() is None:
                self.child.terminate()
                try:
                    self.child.wait(timeout=self.cfg.stop_timeout_sec)
                except subprocess.TimeoutExpired:
                    self.child.kill()
                    self.child.wait(timeout=2)
        except Exception:
            # keep watchdog alive even if terminate/kill fails
            pass
        finally:
            self.child = None

    def restart_bot(self):
        print("[watchdog] restarting bot...", flush=True)
        self.stop_bot()
        self.start_bot()
        # reset downtime marker so we don't immediately loop-restart
        self.down_since = None

    def is_running(self) -> bool:
        return self.child is not None and self.child.poll() is None

    def _handle_exit_signal(self, signum, _frame):
        signame = signal.Signals(signum).name
        print(f"[watchdog] received {signame}; stopping bot", flush=True)
        self.stop_bot()
        raise SystemExit(0)

    def _register_shutdown_hooks(self):
        atexit.register(self.stop_bot)
        for sig in (signal.SIGINT, signal.SIGTERM, getattr(signal, "SIGHUP", None)):
            if sig is not None:
                signal.signal(sig, self._handle_exit_signal)

    def tick(self):
        if self.is_running():
            self.down_since = None
            return

        now = time.monotonic()
        if self.down_since is None:
            self.down_since = now
            print("[watchdog] bot is down; starting 10-minute timer", flush=True)
            return

        down_for = now - self.down_since
        if down_for >= self.cfg.down_threshold_sec:
            self.restart_bot()
            # if it instantly exits, we do not want to spam restarts every tick.
            self.down_since = time.monotonic()
        else:
            print(f"[watchdog] bot down for {int(down_for)}s (waiting for {self.cfg.down_threshold_sec}s)", flush=True)

    def run(self):
        self._register_shutdown_hooks()
        self.start_bot()
        try:
            while True:
                self.tick()
                time.sleep(self.cfg.check_every_sec)
        except KeyboardInterrupt:
            print("[watchdog] received interrupt; stopping bot", flush=True)
        finally:
            self.stop_bot()


def main():
    project_root = Path(__file__).resolve().parent
    cfg = WatchdogConfig(
        bot_cmd=[sys.executable, str(project_root / "main.py")],
        check_every_sec=30,
        down_threshold_sec=600,
        stop_timeout_sec=5,
    )
    BotWatchdog(cfg).run()


if __name__ == "__main__":
    main()
