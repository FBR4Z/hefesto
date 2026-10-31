from __future__ import annotations


from contextlib import suppress
import os
import asyncio
import codecs
import platform
import struct
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
import sys

if sys.platform != "win32":
    # Unix-only; on Windows the shell runs in a ConPTY via pywinpty.
    import fcntl
    import pty
    import termios


from textual import log
from textual.message import Message

from toad.shell_read import shell_read

from toad.widgets.terminal import Terminal

if TYPE_CHECKING:
    from toad.widgets.conversation import Conversation

IS_MACOS = platform.system() == "Darwin"
IS_WINDOWS = sys.platform == "win32"

WINDOWS_DEFAULT_SHELL = "powershell.exe -NoLogo"

# Run (hidden) when a shell starts on Windows. The prompt is replaced by an invisible
# OSC 2025 sequence carrying the working directory, and PSReadLine is removed so the
# command echo comes back as plain text (and can be hidden).
WINDOWS_SHELL_SETUP = {
    "powershell": (
        "Remove-Module PSReadLine -ErrorAction SilentlyContinue; "
        'function global:prompt { "$([char]27)]2025;$($PWD.ProviderPath);$([char]27)\\" }'
    ),
    "cmd": "prompt $E]2025;$P;$E\\",
    "posix": 'PS1=""',
}

# ConPTY positions its cursor absolutely, so each command starts on a cleared screen
# to line up with the new terminal widget that shows its output.
WINDOWS_COMMAND = {
    "powershell": "Clear-Host; {command}",
    "cmd": "cls & {command}",
    "posix": 'clear; {command}; printf "\\e]2025;$(pwd -W 2>/dev/null || pwd);\\e\\\\"',
}


def shell_kind(shell: str) -> str:
    """"powershell", "cmd" or "posix", from a shell command line."""
    from pathlib import PureWindowsPath
    import shlex

    try:
        parts = shlex.split(shell, posix=False)
    except ValueError:
        parts = shell.split()
    if not parts:
        return "powershell"
    name = PureWindowsPath(parts[0].strip("\"'")).stem.lower()
    if name in ("powershell", "pwsh"):
        return "powershell"
    if name == "cmd":
        return "cmd"
    return "posix"


def windows_shell_command(shell: str) -> str:
    """The shell to run on Windows; Unix defaults like /bin/sh fall back to PowerShell."""
    shell = shell.strip()
    if not shell or shell.startswith("/"):
        return WINDOWS_DEFAULT_SHELL
    return shell

def resize_pty(fd, cols, rows):
    """Resize the pseudo-terminal"""
    # Pack the dimensions into the format expected by TIOCSWINSZ
    try:
        size = struct.pack("HHHH", rows, cols, 0, 0)
        fcntl.ioctl(fd, termios.TIOCSWINSZ, size)
    except OSError:
        # Possibly file descriptor closed
        pass


@dataclass
class CurrentWorkingDirectoryChanged(Message):
    """Current working directory has changed in shell."""

    path: str


@dataclass
class ShellFinished(Message):
    """The shell finished."""


class Shell:
    """Responsible for shell interactions in Conversation."""

    def __init__(
        self,
        conversation: Conversation,
        working_directory: str,
        shell="",
        start="",
        hide_start: bool = True,
    ) -> None:
        self.conversation = conversation
        self.working_directory = working_directory

        self.terminal: Terminal | None = None
        self.new_log: bool = False
        if IS_WINDOWS:
            self.shell = windows_shell_command(shell)
            self.kind = shell_kind(self.shell)
            if start.strip() == 'PS1=""':
                # The Unix default; the Windows setup takes care of the prompt.
                start = ""
        else:
            self.shell = shell or os.environ.get("SHELL", "sh")
            self.kind = "posix"
        self.shell_start = start
        self.hide_start = hide_start
        self.master: int | None = None
        """pty file descriptor (Unix)."""
        self._pty: Any = None
        """winpty.PtyProcess (Windows)."""
        self._start_error: str | None = None
        self._task: asyncio.Task | None = None
        self._process: asyncio.subprocess.Process | None = None

        self._finished: bool = False
        self._ready_event: asyncio.Event = asyncio.Event()

        self._hide_echo: set[bytes] = set()
        """A set of byte strings to remove from output."""

        self._hide_output = hide_start
        """Hide all output."""

        self._pid: int | None = None
        """Shell process id"""

    @property
    def is_finished(self) -> bool:
        return self._finished

    def _is_busy(self) -> bool:
        """Check if the shell is busy.

        Called from a thread by `is_busy`.

        Returns:
            `True` if a command is running, or `False` if the shell is waiting for input.

        """
        if self._pid is None:
            return False
        import psutil

        try:
            shell_process = psutil.Process(self._pid)
            children = shell_process.children(recursive=True)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return False
        else:
            return bool(children)

    async def is_busy(self) -> bool:
        """Is there a process running in the shell?

        Returns:
            `True` if a command is running, or `False` if the shell is waiting for input.
        """
        return await asyncio.to_thread(self._is_busy)

    async def wait_for_ready(self) -> None:
        await self._ready_event.wait()

    @property
    def _connected(self) -> bool:
        return self.master is not None or self._pty is not None

    def _resize(self, width: int, height: int) -> None:
        height = max(height, 1)
        if self._pty is not None:
            with suppress(Exception):
                self._pty.setwinsize(height, width)
        elif self.master is not None:
            resize_pty(self.master, width, height)

    async def send(self, command: str, width: int, height: int) -> None:
        await self._ready_event.wait()
        if not self._connected:
            if self._start_error:
                self.conversation.notify(
                    self._start_error, title="Shell", severity="error"
                )
            else:
                print("TTY FD not set")
            return

        if self.terminal is not None:
            self.terminal.finalize()
            self.terminal = None

        try:
            await asyncio.to_thread(self._resize, width, height)
        except OSError:
            pass

        if IS_WINDOWS:
            # (replace, not format: PowerShell commands are full of braces)
            line = WINDOWS_COMMAND[self.kind].replace("{command}", command) + "\n"
            await self.write(line, hide_echo=True)
            return
        get_pwd_command = f"{command};" + r'printf "\e]2025;$(pwd);\e\\"' + "\n"
        await self.write(get_pwd_command, hide_echo=True)

    async def send_input(self, text: str, paste: bool = False) -> None:
        await self._ready_event.wait()
        if not self._connected:
            return
        if paste and self.terminal is not None and self.terminal.state.bracketed_paste:
            text = f"\x1b[200~{text}\x1b[201~"
        await self.write(f"{text}\n", hide_echo=True)

    def start(self) -> None:
        assert self._task is None
        self._task = asyncio.create_task(self.run(), name=repr(self))
        log("shell starting")

    async def interrupt(self) -> None:
        """Interrupt the running command."""
        await self.write(b"\x03")

    def update_size(self, width: int, height: int) -> None:
        """Update the size of the shell pty.

        Args:
            width: Desired width.
            height: Desired height.
        """
        if not self._connected:
            return
        with suppress(OSError):
            self._resize(width, height)

    async def write(
        self, text: str | bytes, hide_echo: bool = False, hide_output: bool = False
    ) -> int:
        if not self._connected:
            return 0
        text_bytes = text.encode("utf-8", "ignore") if isinstance(text, str) else text

        if hide_echo:
            for line in text_bytes.split(b"\n"):
                if line:
                    self._hide_echo.add(line)
        try:
            if self._pty is not None:
                # The console expects Enter as a carriage return.
                console_text = (
                    text_bytes.decode("utf-8", "ignore")
                    .replace("\r\n", "\n")
                    .replace("\n", "\r")
                )
                result = await asyncio.to_thread(self._pty.write, console_text)
            else:
                result = await asyncio.to_thread(os.write, self.master, text_bytes)
        except Exception:
            return 0
        self._hide_output = hide_output
        return result

    async def _start_windows(
        self, current_directory: str, buffer_size: int
    ) -> asyncio.StreamReader | None:
        """Start the shell in a ConPTY, returning a reader for its output."""
        try:
            from winpty import PtyProcess
        except ImportError:
            self._start_error = (
                "O shell integrado precisa do pacote pywinpty. Feche o Hefesto e rode "
                "o instalar-iris.ps1 de novo para atualizar."
            )
            return None

        env = os.environ.copy()
        env["FORCE_COLOR"] = "1"
        env["TERM"] = "xterm-256color"
        env["COLORTERM"] = "truecolor"
        env["TOAD"] = "1"
        env["IRIS"] = "1"
        try:
            width, height = self.conversation.get_terminal_dimensions()
        except Exception:
            width, height = 80, 24
        try:
            self._pty = await asyncio.to_thread(
                PtyProcess.spawn,
                self.shell,
                cwd=current_directory,
                env=env,
                dimensions=(max(height, 1), width),
            )
        except Exception as error:
            self._start_error = (
                f"Não foi possível iniciar o shell ({self.shell}): {error}\n\n"
                "Confira o comando em Configurações → Shell."
            )
            self.conversation.notify(self._start_error, title="Shell", severity="error")
            return None
        self._pid = self._pty.pid

        reader = asyncio.StreamReader(buffer_size)
        loop = asyncio.get_running_loop()
        shell_pty = self._pty

        def pump() -> None:
            """Blocking reads in a thread, handed to the asyncio reader."""
            while True:
                try:
                    data = shell_pty.read(buffer_size)
                except Exception:
                    # EOFError once the shell exits.
                    break
                if data:
                    try:
                        loop.call_soon_threadsafe(reader.feed_data, data.encode("utf-8"))
                    except RuntimeError:
                        # Event loop closed (the app is exiting).
                        return
            with suppress(RuntimeError):
                loop.call_soon_threadsafe(reader.feed_eof)

        import threading

        threading.Thread(target=pump, name="iris-shell", daemon=True).start()
        return reader

    def close(self) -> None:
        """Stop the shell process (Windows; on Unix it ends with the pty)."""
        if self._pty is not None:
            with suppress(Exception):
                self._pty.terminate(force=True)

    async def run(self) -> None:
        current_directory = self.working_directory
        BUFFER_SIZE = 64 * 1024

        if IS_WINDOWS:
            maybe_reader = await self._start_windows(current_directory, BUFFER_SIZE)
            if maybe_reader is None:
                self._ready_event.set()
                return
            reader = maybe_reader
            # Ready once the setup line has run (its prompt reports the directory),
            # so the first command isn't typed while PSReadLine is still loaded.
            asyncio.get_running_loop().call_later(10, self._ready_event.set)
            setup = WINDOWS_SHELL_SETUP[self.kind]
            if self.kind == "posix":
                setup += '; printf "\\e]2025;$(pwd -W 2>/dev/null || pwd);\\e\\\\"'
            await self.write(f"{setup}\n", hide_output=True)
        else:
            maybe_reader = await self._start_unix(current_directory, BUFFER_SIZE)
            if maybe_reader is None:
                self._ready_event.set()
                return
            reader = maybe_reader
            self._ready_event.set()

        if shell_start := self.shell_start.strip():
            shell_start = self.shell_start.strip()
            if not shell_start.endswith("\n"):
                shell_start += "\n"
            await self.write(shell_start, hide_echo=False, hide_output=self.hide_start)

        await self._read_output(reader, BUFFER_SIZE, current_directory)

    async def _start_unix(
        self, current_directory: str, BUFFER_SIZE: int
    ) -> asyncio.StreamReader | None:
        """Start the shell in a pty, returning a reader for its output."""
        master, slave = pty.openpty()
        self.master = master

        flags = fcntl.fcntl(master, fcntl.F_GETFL)
        fcntl.fcntl(master, fcntl.F_SETFL, flags | os.O_NONBLOCK)

        env = os.environ.copy()
        env["FORCE_COLOR"] = "1"
        env["TTY_COMPATIBLE"] = "1"
        env["TERM"] = "xterm-256color"
        env["COLORTERM"] = "truecolor"
        env["TOAD"] = "1"
        env["CLICOLOR"] = "1"

        shell = self.shell

        def setup_pty():
            os.setsid()
            fcntl.ioctl(slave, termios.TIOCSCTTY, 0)

        try:
            _process = await asyncio.create_subprocess_shell(
                shell,
                stdin=slave,
                stdout=slave,
                stderr=slave,
                env=env,
                cwd=current_directory,
                preexec_fn=setup_pty,
            )
        except Exception as error:
            self.conversation.notify(
                f"Unable to start shell: {error}\n\nCheck your settings.",
                title="Shell",
                severity="error",
            )
            return None
        self._pid = _process.pid

        os.close(slave)
        reader = asyncio.StreamReader(BUFFER_SIZE)
        protocol = asyncio.StreamReaderProtocol(reader)

        loop = asyncio.get_event_loop()
        transport, _ = await loop.connect_read_pipe(
            lambda: protocol, os.fdopen(master, "rb", 0)
        )
        return reader

    async def _read_output(
        self, reader: asyncio.StreamReader, BUFFER_SIZE: int, current_directory: str
    ) -> None:
        """Show the shell's output in terminal widgets, until the shell exits."""
        unicode_decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")

        while True:
            data = await shell_read(reader, BUFFER_SIZE)
            if not self._ready_event.is_set() and b"\x1b]2025;" in data:
                self._ready_event.set()

            for string_bytes in list(self._hide_echo):
                remove_bytes = string_bytes
                if remove_bytes in data:
                    remove_start = data.index(remove_bytes)
                    try:
                        next_line = data.index(b"\n", remove_start + len(remove_bytes))
                    except ValueError:
                        data = data.replace(remove_bytes, b"\x1b[2K")
                    else:
                        data = data[:remove_start] + b"\x1b[2K" + data[next_line + 1 :]

                    self._hide_echo.discard(string_bytes)

            if line := unicode_decoder.decode(data, final=not data):
                if self.terminal is None or self.terminal.is_finalized:
                    previous_state = (
                        None if self.terminal is None else self.terminal.state
                    )
                    self.terminal = await self.conversation.new_terminal()
                    # if previous_state is not None:
                    #     self.terminal.set_state(previous_state)
                    self.terminal.set_write_to_stdin(self.write)

                terminal_updated = await self.terminal.write(
                    line, hide_output=self._hide_output
                )
                if terminal_updated and not self.terminal.display:
                    if (
                        self.terminal.alternate_screen
                        or not self.terminal.state.scrollback_buffer.is_blank
                    ):
                        self.terminal.display = True
                new_directory = self.terminal.current_directory
                if new_directory and new_directory != current_directory:
                    current_directory = new_directory
                    self.conversation.post_message(
                        CurrentWorkingDirectoryChanged(current_directory)
                    )
            if (
                self.terminal is not None
                and self.terminal.is_finalized
                and self.terminal.state.scrollback_buffer.is_blank
            ):
                self.terminal.finalize()
                self.terminal = None

            if not data:
                break

        self.master = None
        self._pty = None
        self._ready_event.set()
        self._finished = True
        self.conversation.post_message(ShellFinished())
