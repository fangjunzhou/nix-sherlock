# nix-sherlock

Run Nix on [Stanford Sherlock](https://www.sherlock.stanford.edu/) without root access or a system-wide installation.

`sherlock_nix_chroot.py` creates a rootless, single-user Nix environment with [`nix-user-chroot`](https://github.com/nix-community/nix-user-chroot). The Nix store and build temporary files live in scratch storage by default, while the user profile remains in your home directory. After setup, the script can open a Nix-enabled shell, apply a Home Manager flake, or run one command inside the environment.

Repeated runs reuse the downloaded helper, Nix store, and user profile. The script runs directly on the current Sherlock host, including login nodes.

## Requirements

- An x86_64 Linux node with unprivileged user namespaces enabled
- Python 3
- `bash`, `curl`, and `unshare` on `PATH`
- Outbound HTTPS access to GitHub and `nixos.org` during first-time setup
- One of the following storage configurations:
  - `L_SCRATCH` set in the environment
  - `--scratch-dir PATH`
  - Both `--nix-dir PATH` and `--tmp-dir PATH`

No root privileges are required.

## Quick start

Make sure the script is executable, then run it directly on a Sherlock host:

```bash
chmod +x sherlock_nix_chroot.py
./sherlock_nix_chroot.py
```

On the first run, the script:

1. Downloads the pinned `nix-user-chroot` helper and verifies its SHA-256 checksum.
2. Creates the scratch-backed Nix store and temporary directory.
3. Installs single-user Nix using the official installer.
4. Verifies the installation with `nix --version`.
5. Opens a login shell with Nix available.

Later runs skip installation and reuse the existing environment. Exit the shell normally to leave the chroot:

```bash
exit
```

## Usage

```text
./sherlock_nix_chroot.py [OPTIONS] [-- COMMAND [ARG...]]
```

### Open an interactive shell

```bash
./sherlock_nix_chroot.py
```

The script uses a Home Manager-installed `zsh` when one exists at `~/.nix-profile/bin/zsh`; otherwise it opens `bash`.

### Run a command

Place the command after `--` so its flags are forwarded unchanged:

```bash
./sherlock_nix_chroot.py -- nix develop "$HOME/project"
./sherlock_nix_chroot.py -- nix shell nixpkgs#ripgrep -c rg --version
```

A forwarded command takes precedence over `--no-shell`.

### Apply Home Manager

Pass a flake output to `--hm-flake`:

```bash
./sherlock_nix_chroot.py \
  --hm-flake "$HOME/nixos-config#sherlock"
```

This runs the equivalent of:

```bash
nix run github:nix-community/home-manager -- switch --flake PATH#NAME
```

After Home Manager finishes, the script opens the interactive shell unless `--no-shell` or a forwarded command was supplied.

### Set up without opening a shell

```bash
./sherlock_nix_chroot.py --no-shell
```

This is useful for installation checks and non-interactive provisioning.

### Use explicit storage paths

`--scratch-dir` changes both scratch-derived defaults:

```bash
./sherlock_nix_chroot.py --scratch-dir /path/to/scratch
```

Or configure the store and temporary directory independently:

```bash
./sherlock_nix_chroot.py \
  --nix-dir /path/to/nix \
  --tmp-dir /path/to/nix-tmp
```

When both paths are explicit, `L_SCRATCH` is not required.

## Options

| Option | Description | Default |
| --- | --- | --- |
| `--hm-flake PATH#NAME` | Apply a Home Manager flake output after Nix setup | Disabled |
| `--scratch-dir PATH` | Root used to derive the Nix and temporary paths | `$L_SCRATCH` |
| `--nix-dir PATH` | Host directory exposed as `/nix` | `<scratch-dir>/nix` |
| `--tmp-dir PATH` | Build and command temporary directory | `<scratch-dir>/nix-tmp` |
| `--runtime-home PATH` | `HOME` inside the chroot and location of the user Nix profile | Current user home |
| `--nix-user-chroot-dir PATH` | Cache directory for `nix-user-chroot` | `~/.local/bin` |
| `--no-shell` | Exit after setup and optional Home Manager activation | Disabled |
| `-h`, `--help` | Show command-line help | — |

## State and persistence

The default layout separates large or temporary data from home-directory state:

| State | Default location | Permissions set by the script |
| --- | --- | --- |
| Nix store and configuration | `$L_SCRATCH/nix` | `0755` |
| Temporary build data | `$L_SCRATCH/nix-tmp` | `0700` |
| Nix profile, including `~/.nix-profile` | Current user home | Runtime home is set to `0700` |
| Versioned `nix-user-chroot` helper | `~/.local/bin/nix-user-chroot-2.1.1` | Executable |

The stable `~/.local/bin/nix-user-chroot` symlink is created only when that path is unused; an existing file or link is never replaced.

Persistence and visibility of the Nix store follow the policies of the directory selected by `L_SCRATCH`, `--scratch-dir`, or `--nix-dir`. Removing that directory removes the store and may require packages to be installed again.

## How it works

Each invocation performs these checks and actions:

1. Validates the host architecture and required commands.
2. Downloads or revalidates the pinned `nix-user-chroot` binary.
3. Confirms that the current host permits unprivileged user and PID namespaces.
4. Exposes the selected backing directory as `/nix` inside the user chroot.
5. Installs Nix only when the runtime home has no executable Nix profile.
6. Applies Home Manager, executes the forwarded command, or opens a login shell.

The child environment inherits useful host settings such as `PATH`, proxy variables, and terminal configuration. Host linker overrides (`LD_LIBRARY_PATH`, `LD_PRELOAD`, `NIX_LD`, and `NIX_LD_LIBRARY_PATH`) are removed to prevent Sherlock modules from injecting incompatible libraries into Nix binaries.

Nix is configured with:

```ini
sandbox = false
extra-experimental-features = nix-command flakes
```

The Nix build sandbox is disabled because Nix itself is already running inside an unprivileged user chroot. This means builds do not receive the additional filesystem isolation normally provided by the Nix sandbox.

## Troubleshooting

### `set L_SCRATCH or pass --scratch-dir`

The script cannot derive one or both storage paths. Set `L_SCRATCH`, pass `--scratch-dir`, or explicitly provide both `--nix-dir` and `--tmp-dir`.

### `unprivileged user namespaces are unavailable on this host`

The current node or account cannot create the namespaces required by `nix-user-chroot`. Try a Sherlock host where unprivileged user namespaces are enabled or contact the cluster administrators.

### A required command is missing

The error names the missing executable. Ensure `curl`, `unshare`, `bash`, and Python 3 are available before running the script again.

### The helper checksum fails

The downloaded `nix-user-chroot` binary did not match the checksum pinned in the script and was not installed. Check the network path or proxy, remove any incomplete download if one remains, and retry. Do not bypass checksum verification.

### Resetting the installation

The Nix store is the directory selected by `--nix-dir` or derived from the scratch root. Treat it as state: deleting it removes installed store objects. The runtime home contains the corresponding user profile and is managed separately.
