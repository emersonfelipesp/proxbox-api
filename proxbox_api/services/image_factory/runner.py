"""Async Packer subprocess runner."""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass, field
from pathlib import Path

from proxbox_api.runtime_settings import get_str
from proxbox_api.services.image_factory.logs import (
    PackerEvent,
    normalize_machine_readable_line,
    normalize_timestamp_ui_line,
    scrub_text,
)


@dataclass(frozen=True)
class CommandResult:
    command: tuple[str, ...]
    exit_code: int
    stdout: list[str] = field(default_factory=list)
    stderr: list[str] = field(default_factory=list)
    events: list[PackerEvent] = field(default_factory=list)


class PackerCommandError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        exit_code: int | None = None,
        output: Iterable[str] = (),
    ) -> None:
        super().__init__(message)
        self.exit_code = exit_code
        self.output = list(output)


class PackerRunner:
    def __init__(
        self,
        *,
        binary: str | None = None,
        env: dict[str, str] | None = None,
        secrets: Iterable[str] = (),
    ) -> None:
        self.binary = binary or get_str(
            settings_key="packer_binary",
            env="PROXBOX_PACKER_BINARY",
            default="packer",
        )
        self.env = dict(env or {})
        self.secrets = tuple(secret for secret in secrets if secret)
        self._processes: dict[str, asyncio.subprocess.Process] = {}
        self._lock = asyncio.Lock()

    async def init(self, workdir: Path) -> CommandResult:
        return await self._run_command(
            workdir,
            (self.binary, "init", "."),
            phase="init",
        )

    async def validate(self, workdir: Path, var_file: Path) -> CommandResult:
        return await self._run_command(
            workdir,
            (
                self.binary,
                "validate",
                "-machine-readable",
                f"-var-file={var_file.name}",
                ".",
            ),
            phase="validate",
        )

    async def build(self, workdir: Path, var_file: Path) -> AsyncIterator[PackerEvent]:
        command = (
            self.binary,
            "build",
            "-color=false",
            "-timestamp-ui",
            "-on-error=cleanup",
            f"-var-file={var_file.name}",
            ".",
        )
        build_id = workdir.name
        try:
            process = await self._create_process(command, workdir)
        except FileNotFoundError as error:
            raise PackerCommandError(
                f"Packer binary not found: {self.binary}",
                exit_code=127,
            ) from error

        async with self._lock:
            self._processes[build_id] = process

        queue: asyncio.Queue[tuple[str, str | None]] = asyncio.Queue()

        async def pump(reader: asyncio.StreamReader | None, stream: str) -> None:
            if reader is None:
                await queue.put((stream, None))
                return
            while True:
                raw = await reader.readline()
                if not raw:
                    await queue.put((stream, None))
                    return
                await queue.put((stream, raw.decode(errors="replace").rstrip("\n")))

        pumps = [
            asyncio.create_task(pump(process.stdout, "stdout")),
            asyncio.create_task(pump(process.stderr, "stderr")),
        ]
        completed_streams = 0
        output: list[str] = []
        try:
            while completed_streams < len(pumps):
                stream, line = await queue.get()
                if line is None:
                    completed_streams += 1
                    continue
                output.append(line)
                event = normalize_timestamp_ui_line(line, stream=stream, secrets=self.secrets)
                if event.data.get("message"):
                    yield event
            exit_code = await process.wait()
            if exit_code != 0:
                raise PackerCommandError(
                    "packer build failed",
                    exit_code=exit_code,
                    output=[scrub_text(line, self.secrets) for line in output],
                )
        finally:
            for task in pumps:
                if not task.done():
                    task.cancel()
            async with self._lock:
                self._processes.pop(build_id, None)

    async def cancel(self, build_id: str) -> None:
        async with self._lock:
            process = self._processes.get(build_id)
        if process is None or process.returncode is not None:
            return
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
        except TimeoutError:
            process.kill()
            await process.wait()

    async def _run_command(
        self,
        workdir: Path,
        command: tuple[str, ...],
        *,
        phase: str,
    ) -> CommandResult:
        build_id = workdir.name
        try:
            process = await self._create_process(command, workdir)
        except FileNotFoundError:
            return CommandResult(
                command=command,
                exit_code=127,
                stderr=[f"Packer binary not found: {self.binary}"],
            )
        async with self._lock:
            self._processes[build_id] = process
        try:
            stdout_raw, stderr_raw = await process.communicate()
        finally:
            async with self._lock:
                self._processes.pop(build_id, None)

        stdout = stdout_raw.decode(errors="replace").splitlines() if stdout_raw else []
        stderr = stderr_raw.decode(errors="replace").splitlines() if stderr_raw else []
        events = [
            normalize_machine_readable_line(
                line, phase=phase, stream="stdout", secrets=self.secrets
            )
            for line in stdout
        ]
        events.extend(
            normalize_machine_readable_line(
                line, phase=phase, stream="stderr", secrets=self.secrets
            )
            for line in stderr
        )
        return CommandResult(
            command=command,
            exit_code=process.returncode or 0,
            stdout=[scrub_text(line, self.secrets) for line in stdout],
            stderr=[scrub_text(line, self.secrets) for line in stderr],
            events=events,
        )

    async def _create_process(
        self,
        command: tuple[str, ...],
        workdir: Path,
    ) -> asyncio.subprocess.Process:
        env = os.environ.copy()
        env.update(self.env)
        return await asyncio.create_subprocess_exec(
            *command,
            cwd=str(workdir),
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
