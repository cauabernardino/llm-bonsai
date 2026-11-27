import shlex
import subprocess

from llm_bonsai.errors import LabError


def run(*cmd: str, capture: bool = True, stdin: str | None = None) -> str:
    """Run a command and return its stdout, turning failures into LabErrors.

    With capture=False the output goes straight to the terminal, so long installs show
    progress. Secrets only ever go in on stdin, so a failing command is shown in full.
    """
    try:
        done = subprocess.run(cmd, check=True, text=True, capture_output=capture, input=stdin)
    except FileNotFoundError as error:
        raise LabError(f"{cmd[0]} not found on PATH") from error
    except subprocess.CalledProcessError as error:
        output = f"{error.stdout or ''}{error.stderr or ''}".strip()
        detail = f":\n{output[-1500:]}" if output else ""
        raise LabError(f"`{shlex.join(cmd)}` exited with {error.returncode}{detail}") from error
    return done.stdout if capture else ""
