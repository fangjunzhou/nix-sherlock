#!/usr/bin/env python3
"""Provide a persistent, rootless Nix environment on Stanford Sherlock.

The script runs directly on the current Sherlock host, including login nodes.
It uses ``nix-user-chroot`` to mount a user-owned directory at ``/nix`` without
root privileges, installs or reuses single-user Nix, and then applies Home
Manager, runs a command, or opens a login shell.

State is deliberately separated:

* ``nix_dir`` contains the Nix store and is normally derived from ``L_SCRATCH``.
* ``runtime_home`` contains ``~/.nix-profile`` and defaults to the user's home.
* ``helper_dir`` caches the pinned ``nix-user-chroot`` executable.
* ``tmp_dir`` keeps temporary build data off the home filesystem.

The helper download is SHA-256 verified before execution. The initial Nix
installer is fetched from nixos.org over HTTPS. Nix build sandboxing is disabled
because commands already run inside an unprivileged user namespace.
"""

import argparse
import hashlib
import logging
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple


# nix-user-chroot is a static x86_64 helper. Update the version and checksum
# together; cached copies are rehashed before the script executes them.
NIX_USER_CHROOT_VERSION: str = "2.1.1"
NIX_USER_CHROOT_SHA256: str = (
    "e6daa6a036e00b939531e5c11e6dcd891140813a09964fd12a64112de500fc15"
)
NIX_USER_CHROOT_URL = (
    "https://github.com/nix-community/nix-user-chroot/releases/download/"
    "{version}/nix-user-chroot-bin-{version}-x86_64-unknown-linux-musl"
).format(version=NIX_USER_CHROOT_VERSION)

# These settings are supplied both through nix.conf and NIX_CONFIG. Nix cannot
# create its normal build sandbox from this unprivileged chroot, while flakes
# and the modern command interface are required by supported workflows.
NIX_CONFIG: str = "sandbox = false\nextra-experimental-features = nix-command flakes"
# A login shell may source host/module configuration and recreate linker
# overrides after run_in_chroot sanitizes its initial environment. Unset them
# again before sourcing the Nix profile so Nix always starts clean.
NIX_PROFILE_INIT: str = (
    "unset LD_LIBRARY_PATH LD_PRELOAD NIX_LD NIX_LD_LIBRARY_PATH\n"
    '. "$HOME/.nix-profile/etc/profile.d/nix.sh"'
)

DESCRIPTION: str = """Set up or reuse a single-user Nix installation whose /nix
directory is backed by Sherlock local scratch, then enter a Nix-enabled shell.
No root privileges or system-wide Nix installation are required.

The script runs directly on the current host, including Sherlock login nodes.
On first use it downloads a checksum-verified nix-user-chroot helper and the
official Nix installer. Later invocations reuse the helper, Nix store, and user
profile."""

EPILOG: str = """Anything after -- is executed inside the Nix chroot instead of
opening a shell. A command takes precedence over --no-shell.

Examples:
  ./sherlock_nix_chroot.py
  ./sherlock_nix_chroot.py --hm-flake "$HOME/nixos-config#sherlock"
  ./sherlock_nix_chroot.py -- nix develop "$HOME/project"
  ./sherlock_nix_chroot.py --no-shell

Requirements:
  - Python 3 on an x86_64 Linux node with user namespaces enabled.
  - L_SCRATCH, --scratch-dir, or both --nix-dir and --tmp-dir for storage.
  - curl, unshare, bash, and outbound HTTPS access.

Operational notes:
  sandboxing is disabled because this is already an unprivileged user chroot.
  Removing the scratch-backed Nix directory removes the Nix store; packages may
  then need to be reinstalled."""

# Configure only this named logger. Disabling propagation prevents duplicate
# messages when an importing process has also configured the root logger.
logger: logging.Logger = logging.getLogger("sherlock-nix")
logger.setLevel(logging.INFO)
logger.propagate = False
if not logger.handlers:
    _handler: logging.Handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("[sherlock-nix] %(message)s"))
    logger.addHandler(_handler)


class Options(argparse.Namespace):
    """Typed values populated by :func:`build_parser`."""

    hm_flake: str
    scratch_dir: Optional[Path]
    nix_dir: Optional[Path]
    tmp_dir: Optional[Path]
    runtime_home: Optional[Path]
    helper_dir: Optional[Path]
    no_shell: bool


def build_parser() -> argparse.ArgumentParser:
    """Construct the public command-line interface.

    Path options are converted to ``Path`` instances. Option abbreviation is
    disabled to keep forwarded command intent explicit.
    """
    parser = argparse.ArgumentParser(
        prog="sherlock_nix_chroot.py",
        usage="%(prog)s [OPTIONS] [-- COMMAND [ARG...]]",
        description=DESCRIPTION,
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        allow_abbrev=False,
    )
    parser.add_argument(
        "--hm-flake",
        metavar="PATH#NAME",
        default="",
        help="apply this Home Manager flake output after Nix setup",
    )
    # The environment supplies the default, while an explicit command-line value
    # overrides it normally through argparse.
    parser.add_argument(
        "--scratch-dir",
        type=Path,
        default=os.environ.get("L_SCRATCH") or None,
        metavar="PATH",
        help="scratch root used for path defaults (default: $L_SCRATCH)",
    )
    parser.add_argument(
        "--nix-dir",
        type=Path,
        metavar="PATH",
        help="backing directory for /nix (default: <scratch-dir>/nix)",
    )
    parser.add_argument(
        "--tmp-dir",
        type=Path,
        metavar="PATH",
        help="temporary directory (default: <scratch-dir>/nix-tmp)",
    )
    parser.add_argument(
        "--runtime-home",
        type=Path,
        metavar="PATH",
        help="HOME inside the chroot (default: current user home)",
    )
    parser.add_argument(
        "--helper-dir",
        type=Path,
        metavar="PATH",
        help="nix-user-chroot cache directory (default: ~/.local/bin)",
    )
    parser.add_argument(
        "--no-shell",
        action="store_true",
        help="exit after setup (and Home Manager, if requested)",
    )
    return parser


def parse_arguments(arguments: Sequence[str]) -> Tuple[Options, List[str]]:
    """Parse script options and return untouched chroot command arguments.

    Tokens before the first ``--`` are parsed by argparse. Tokens after it keep
    their original order and spelling for forwarding through ``bash -lc``.
    """
    argument_list: List[str] = list(arguments)
    # Split manually so command flags such as ``nix --help`` are never mistaken
    # for this script's options. Only the first delimiter has special meaning.
    try:
        separator: int = argument_list.index("--")
    except ValueError:
        option_arguments: List[str] = argument_list
        command_arguments: List[str] = []
    else:
        option_arguments = argument_list[:separator]
        command_arguments = argument_list[separator + 1 :]

    # Ask argparse to populate the typed namespace in place; this gives callers
    # stable, documented attributes instead of an unstructured Namespace.
    options = Options()
    build_parser().parse_args(option_arguments, namespace=options)
    return options, command_arguments


def require_command(name: str) -> str:
    """Resolve an external command through PATH or raise a clear error early."""
    path = shutil.which(name)
    if path is None:
        raise RuntimeError("required command not found: {}".format(name))
    return path


def file_sha256(path: Path) -> str:
    """Calculate a file's lowercase SHA-256 digest in bounded-memory chunks."""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def prepare_helper(helper_dir: Path, curl_path: str) -> Path:
    """Return a verified, executable ``nix-user-chroot`` helper.

    A matching cached version is reused. Otherwise the pinned release is
    downloaded to a private temporary file, checksum verified, chmodded, and
    atomically moved into place. Failed or interrupted downloads are removed.
    """
    # Version the real executable so future upgrades can coexist with older
    # cached releases and the stable convenience symlink below.
    helper_path = helper_dir / "nix-user-chroot-{}".format(
        NIX_USER_CHROOT_VERSION
    )
    helper_dir.mkdir(parents=True, exist_ok=True)

    # A cached executable is trusted only after hashing its current contents.
    helper_valid = (
        helper_path.is_file()
        and file_sha256(helper_path) == NIX_USER_CHROOT_SHA256
    )
    if not helper_valid:
        # mkstemp prevents concurrent runs from clobbering one another and
        # avoids predictable-name/symlink attacks in the helper directory.
        descriptor, download_name = tempfile.mkstemp(
            prefix=".{}.download.".format(helper_path.name), dir=str(helper_dir)
        )
        os.close(descriptor)
        download_path = Path(download_name)
        try:
            logger.info("downloading nix-user-chroot %s", NIX_USER_CHROOT_VERSION)
            subprocess.run(
                [
                    curl_path,
                    "-fL",
                    "--retry",
                    "3",
                    "--retry-delay",
                    "2",
                    str(NIX_USER_CHROOT_URL),
                    "-o",
                    str(download_path),
                ],
                check=True,
            )
            # Never install or execute bytes that do not match the pinned digest.
            if file_sha256(download_path) != NIX_USER_CHROOT_SHA256:
                raise RuntimeError("nix-user-chroot checksum verification failed")
            # The temporary file lives beside the destination, making replace
            # atomic on the same filesystem. Set executable mode before publish.
            download_path.chmod(0o755)
            os.replace(str(download_path), str(helper_path))
        # Always remove an incomplete download. After os.replace this is a
        # harmless no-op because the temporary pathname no longer exists.
        finally:
            try:
                download_path.unlink()
            except FileNotFoundError:
                pass

    # The convenience symlink is created only when no file or link occupies the
    # stable name; this avoids overwriting a user-managed or even dangling link.
    stable_link = helper_dir / "nix-user-chroot"
    if not os.path.lexists(str(stable_link)):
        stable_link.symlink_to(helper_path.name)

    return helper_path


def ensure_directory(path: Path, mode: int) -> None:
    """Create ``path`` and enforce ``mode`` even when it already exists.

    ``Path.mkdir(mode=...)`` applies the mode only to new directories, so the
    explicit chmod keeps permissions deterministic across repeated runs.
    """
    path.mkdir(mode=mode, parents=True, exist_ok=True)
    path.chmod(mode)


def run_in_chroot(
    helper_path: Path,
    nix_dir: Path,
    runtime_home: Path,
    tmp_dir: Path,
    arguments: Sequence[str],
    check: bool = True,
) -> subprocess.CompletedProcess:
    """Execute a command with ``nix_dir`` exposed as ``/nix``.

    The child inherits most of the caller's environment, but host linker
    overrides are removed and HOME, TMPDIR, and NIX_CONFIG are replaced. With
    ``check=False``, callers may inspect an expected nonzero status; otherwise
    this wrapper raises ``CalledProcessError``.
    """
    # Inherit PATH, proxy settings, terminal variables, and other host settings,
    # but force the state-bearing values that must refer to the chroot environment.
    environment: Dict[str, str] = os.environ.copy()
    # Sherlock software modules may inject host libraries that are incompatible
    # with binaries from the Nix store. Remove both standard dynamic-linker
    # overrides and nix-ld overrides before starting nix-user-chroot.
    for variable in (
        "LD_LIBRARY_PATH",
        "LD_PRELOAD",
        "NIX_LD",
        "NIX_LD_LIBRARY_PATH",
    ):
        environment.pop(variable, None)
    environment.update(
        {
            "HOME": str(runtime_home),
            "TMPDIR": str(tmp_dir),
            "NIX_CONFIG": NIX_CONFIG,
        }
    )
    # nix-user-chroot expects the backing store first, followed by the command
    # to execute inside the resulting mount/user namespace.
    command: List[str] = [str(helper_path), str(nix_dir)] + list(arguments)
    # Keep check handling here rather than in each caller so probe commands can
    # opt into nonzero results without weakening normal error propagation.
    completed: subprocess.CompletedProcess = subprocess.run(
        command, env=environment, check=False
    )
    if check and completed.returncode != 0:
        raise subprocess.CalledProcessError(completed.returncode, command)
    return completed


def main(arguments: Sequence[str]) -> int:
    """Coordinate persistent paths, Nix bootstrap, and the final action.

    Every operation that needs ``/nix`` is routed through :func:`run_in_chroot`
    with the same backing store, home, and temporary path.
    """
    # Parse the public options immediately and preserve forwarded command tokens.
    # command_arguments remains a token list throughout; it is never re-parsed by
    # Python or combined into a shell command string.
    options, command_arguments = parse_arguments(arguments)
    hm_flake = options.hm_flake
    open_shell = not options.no_shell

    # The pinned helper release is specifically the x86_64 Linux-musl binary.
    if platform.machine() != "x86_64":
        raise RuntimeError("this script currently supports x86_64 nodes only")

    # Scratch supplies only the defaults: callers may independently place the
    # Nix store and temporary directory elsewhere with their explicit options.
    # Consequently scratch is required only for a path whose override is absent.
    scratch: Optional[Path] = options.scratch_dir
    if options.nix_dir is None:
        if scratch is None:
            raise RuntimeError(
                "set L_SCRATCH or pass --scratch-dir when --nix-dir is omitted"
            )
        nix_dir = scratch / "nix"
    else:
        nix_dir = options.nix_dir

    if options.tmp_dir is None:
        if scratch is None:
            raise RuntimeError(
                "set L_SCRATCH or pass --scratch-dir when --tmp-dir is omitted"
            )
        tmp_dir = scratch / "nix-tmp"
    else:
        tmp_dir = options.tmp_dir

    # Keep the helper and user profile in the persistent home filesystem by
    # default, while the much larger Nix store and build data live on scratch.
    home = Path.home()
    helper_dir = options.helper_dir or home / ".local" / "bin"
    runtime_home = options.runtime_home or home

    # Resolve all host-side dependencies before downloading or creating state so
    # a missing executable produces an immediate, actionable failure.
    curl_path = require_command("curl")
    unshare_path = require_command("unshare")
    require_command("bash")

    helper_path = prepare_helper(helper_dir, curl_path)

    # Probe the kernel feature used by nix-user-chroot itself. Merely finding the
    # unshare executable does not guarantee that the cluster permits user/PID
    # namespaces for this account or node.
    namespace_test = subprocess.run(
        [unshare_path, "--user", "--pid", "true"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if namespace_test.returncode != 0:
        raise RuntimeError(
            "unprivileged user namespaces are unavailable on this host"
        )

    # /nix and its configuration must be traversable by Nix processes. Runtime
    # HOME and TMPDIR may contain user data, so keep those private at mode 0700.
    ensure_directory(nix_dir, 0o755)
    ensure_directory(tmp_dir, 0o700)
    ensure_directory(runtime_home, 0o700)
    nix_config_dir = nix_dir / "etc" / "nix"
    ensure_directory(nix_config_dir, 0o755)
    # Rewrite the small authoritative configuration on every run so a script
    # upgrade changes existing stores as well as newly initialized ones.
    (nix_config_dir / "nix.conf").write_text(NIX_CONFIG + "\n", encoding="utf-8")

    # Bundle the invariant mount/environment arguments shared by every command.
    chroot_context = (helper_path, nix_dir, runtime_home, tmp_dir)
    # The per-user profile executable is the installation marker. Probe inside
    # the chroot because the host cannot see the same /nix mount arrangement.
    nix_present = run_in_chroot(
        *chroot_context,
        ["bash", "-c", 'test -x "$HOME/.nix-profile/bin/nix"'],
        check=False
    ).returncode == 0

    if nix_present:
        logger.info("reusing the Nix installation in %s", nix_dir)
    else:
        # Installation is a first-use operation. pipefail ensures a failed HTTPS
        # download cannot be hidden by the shell process consuming the pipeline.
        logger.info("installing Nix into %s", nix_dir)
        run_in_chroot(
            *chroot_context,
            [
                "bash",
                "-c",
                "set -o pipefail; curl -fL https://nixos.org/nix/install "
                "| sh -s -- --no-daemon",
            ]
        )

    # Source Nix's profile script and execute the binary as a post-install/reuse
    # health check before attempting optional higher-level operations.
    run_in_chroot(
        *chroot_context,
        ["bash", "-lc", NIX_PROFILE_INIT + "\nnix --version"]
    )

    if hm_flake:
        # Pass the flake reference as $1 instead of interpolating it into shell
        # source, preserving spaces and preventing shell metacharacter expansion.
        logger.info("applying Home Manager configuration %s", hm_flake)
        run_in_chroot(
            *chroot_context,
            [
                "bash",
                "-lc",
                NIX_PROFILE_INIT
                + '\nexec nix run github:nix-community/home-manager -- switch --flake "$1"',
                "bash",
                hm_flake,
            ]
        )

    # A command after -- takes precedence over shell/no-shell selection. Supplying
    # tokens as positional parameters lets `exec "$@"` preserve argument bounds.
    if command_arguments:
        run_in_chroot(
            *chroot_context,
            [
                "bash",
                "-lc",
                NIX_PROFILE_INIT + '\nexec "$@"',
                "bash",
            ]
            + command_arguments
        )
    elif open_shell:
        # Prefer a Home Manager-installed zsh when available; otherwise bash is a
        # dependable fallback. exec makes the interactive shell the final process
        # so its exit status and lifetime flow directly back to this script.
        logger.info("entering the Nix environment; exit to leave the chroot")
        run_in_chroot(
            *chroot_context,
            [
                "bash",
                "-lc",
                NIX_PROFILE_INIT
                + "\n"
                + 'if [[ -x "$HOME/.nix-profile/bin/zsh" ]]; then\n'
                + '    exec "$HOME/.nix-profile/bin/zsh" -l\n'
                + "fi\n"
                + "exec bash -l",
            ]
        )
    else:
        logger.info("setup complete")

    return 0


def subprocess_exit_code(return_code: int) -> int:
    """Translate a signal-terminated child status to conventional shell form.

    ``subprocess`` represents termination by signal N as ``-N``. Shells normally
    expose that outcome as ``128 + N``, which is equivalent to this expression.
    """
    return return_code if return_code >= 0 else 128 - return_code


if __name__ == "__main__":
    # Expected operational errors are concise and traceback-free. Preserve child
    # statuses when possible, and use the conventional 130 status for Ctrl-C.
    try:
        sys.exit(main(sys.argv[1:]))
    except RuntimeError as error:
        logger.error("error: %s", error)
        sys.exit(1)
    except subprocess.CalledProcessError as error:
        sys.exit(subprocess_exit_code(error.returncode))
    except KeyboardInterrupt:
        sys.exit(130)
    except OSError as error:
        logger.error("error: %s", error)
        sys.exit(1)
