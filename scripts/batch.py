#!/usr/bin/env python3
import argparse
import asyncio
import os
import random
import subprocess
import sys
import threading
import traceback
from datetime import datetime
from typing import Dict, Optional

from dotenv import load_dotenv

from config.persona_utils import PersonaManager
from meetingbaas_pipecat.utils.logger import configure_logger

load_dotenv(override=True)

logger = configure_logger()


def validate_url(url):
    """Validates the URL format, ensuring it starts with https://"""
    if not url.startswith("https://"):
        raise ValueError("URL must start with https://")
    return url


def get_user_input(prompt, validator=None):
    while True:
        user_input = input(prompt).strip()
        if validator:
            try:
                return validator(user_input)
            except ValueError as e:
                logger.warning(f"Invalid input received: {e}")
        else:
            return user_input


class BotManager:
    def __init__(self):
        self.processes: Dict = {}
        self.start_time = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.shutdown_event = asyncio.Event()
        self.selected_persona_name = None

    def run_command(self, command: list[str], name: str) -> Optional[subprocess.Popen]:
        """Run a command and store the process"""
        try:
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                universal_newlines=True,
            )

            def log_output(stream, prefix):
                for line in stream:
                    line = line.strip()
                    if line:
                        if "ERROR" in line:
                            logger.error(f"{prefix}: {line}")
                        elif "WARNING" in line:
                            logger.warning(f"{prefix}: {line}")
                        elif "SUCCESS" in line:
                            logger.success(f"{prefix}: {line}")
                        else:
                            logger.info(f"{prefix}: {line}")

            threading.Thread(
                target=log_output, args=(process.stdout, f"{name}"), daemon=True
            ).start()
            threading.Thread(
                target=log_output, args=(process.stderr, f"{name}"), daemon=True
            ).start()

            self.processes[name] = {"process": process, "command": command}
            return process
        except Exception as e:
            logger.error(f"Failed to start {name}: {e}")
            logger.error(
                "".join(traceback.format_exception(type(e), e, e.__traceback__))
            )
            return None

    async def cleanup(self):
        """Cleanup all processes"""
        try:
            for name, process_info in self.processes.items():
                logger.info(f"Terminating process: {name}")
                process = process_info["process"]
                try:
                    process.terminate()
                    await asyncio.sleep(1)
                    if process.poll() is None:
                        process.kill()
                    logger.success(f"Process {name} terminated successfully")
                except Exception as e:
                    logger.error(f"Error terminating process {name}: {e}")

            logger.success("Cleanup completed successfully")
        except Exception as e:
            logger.error(f"Error during cleanup: {e}")

    async def monitor_processes(self) -> None:
        """Monitor running processes and handle failures"""
        while not self.shutdown_event.is_set():
            try:
                for name, process_info in list(self.processes.items()):
                    process = process_info["process"]
                    if process.poll() is not None:
                        logger.warning(
                            f"Process {name} exited with code: {process.returncode}"
                        )
                await asyncio.sleep(1)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error monitoring processes: {e}")
                await asyncio.sleep(1)

    async def async_main(self) -> None:
        parser = argparse.ArgumentParser(
            description="Run a single bot with random persona selection"
        )
        parser.add_argument(
            "--persona",
            help="Specific persona name to use. If not provided, a random persona will be selected.",
        )
        parser.add_argument(
            "--port",
            type=int,
            default=int(os.getenv("PORT", 8765)),
            help="Port number (default: 8765 or PORT env variable)",
        )
        parser.add_argument(
            "--meeting-url", help="The meeting URL (must start with https://)"
        )
        args = parser.parse_args()

        meeting_url = args.meeting_url
        if not meeting_url:
            meeting_url = get_user_input(
                "Enter the meeting URL (must start with https://): ", validate_url
            )

        try:
            logger.info("Starting bot with random persona selection...")

            # Handle persona selection
            available_personas = PersonaManager().list_personas()
            
            if args.persona:
                if args.persona not in available_personas:
                    raise ValueError(f"Persona '{args.persona}' not found in available personas")
                self.selected_persona_name = args.persona
            else:
                self.selected_persona_name = random.choice(available_personas)

            persona = PersonaManager().get_persona(self.selected_persona_name)
            bot_prompt = persona["prompt"]
            
            logger.warning(f"Selected persona: {self.selected_persona_name}")
            logger.warning(f"System prompt: {bot_prompt}")

            # Start bot
            bot_port = args.port
            bot_name = "bot"
            bot_process = self.run_command(
                [
                    "poetry",
                    "run",
                    "bot",
                    "-p",
                    str(bot_port),
                    "--system-prompt",
                    bot_prompt,
                    "--persona-name",
                    self.selected_persona_name,
                    "--voice-id",
                    "40104aff-a015-4da1-9912-af950fbec99e",
                ],
                bot_name,
            )

            if not bot_process:
                logger.error("Failed to start bot")
                return

            await asyncio.sleep(1)

            # Start meeting
            meeting_name = "meeting"
            meeting_process = self.run_command(
                [
                    "poetry",
                    "run",
                    "meetingbaas",
                    "--meeting-url",
                    meeting_url,
                    "--persona-name",
                    self.selected_persona_name,
                ],
                meeting_name,
            )

            if not meeting_process:
                logger.error("Failed to start meeting")
                return

            logger.success("Successfully started bot and meeting processes")
            logger.info("Press Ctrl+C to stop all processes")

            # Start process monitor
            monitor_task = asyncio.create_task(self.monitor_processes())

            try:
                await self.shutdown_event.wait()
            except asyncio.CancelledError:
                logger.info("\nReceived shutdown signal")
            finally:
                self.shutdown_event.set()
                await monitor_task

        except KeyboardInterrupt:
            logger.info("\nReceived shutdown signal (Ctrl+C)")
        except Exception as e:
            logger.error(f"Unexpected error: {e}")
        finally:
            await self.cleanup()
            logger.success("Cleanup completed successfully")

    def main(self) -> None:
        """Main entry point with proper signal handling"""
        try:
            if sys.platform != "win32":
                # Set up signal handlers for Unix-like systems
                import signal

                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)

                def signal_handler():
                    self.shutdown_event.set()

                loop.add_signal_handler(signal.SIGINT, signal_handler)
                loop.add_signal_handler(signal.SIGTERM, signal_handler)

                try:
                    loop.run_until_complete(self.async_main())
                finally:
                    loop.close()
            else:
                # Windows doesn't support loop.add_signal_handler
                asyncio.run(self.async_main())
        except Exception as e:
            logger.exception(f"Fatal error in main program: {e}")
            sys.exit(1)


if __name__ == "__main__":
    manager = BotManager()
    manager.main()
