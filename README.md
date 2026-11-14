# SNode.C packages

This repository builds and signs the SNode.C ecosystem's distribution packages. `main` owns the schema-1 registry, target catalog, and wave workflow; source repositories own their declarations and build hooks. core-ci supplies an ordered plan and serializes waves. The `feed` branch is the production repository; `feed-staging` is used for qualification. Each feed has one root commit and retains the newest two publications per package and target. Republishing the same version/revision replaces that publication.

## Installation

The production base URL is `https://raw.githubusercontent.com/SNodeC/Packages/feed`. Replace `feed` with `feed-staging` to use staging. Run installation commands as root or through sudo.

### Debian, Ubuntu and Raspberry Pi OS

Install `ca-certificates`, `curl`, and `gpg`; choose your distribution codename (`trixie`, `forky`, `sid`, `noble`, `resolute`, or Raspberry Pi OS `bookworm`).

```sh
base=https://raw.githubusercontent.com/SNodeC/Packages/feed
codename=trixie
curl -fsSL "$base/keys/packages-gpg.asc" | gpg --dearmor -o /usr/share/keyrings/snodec.gpg
printf 'deb [signed-by=/usr/share/keyrings/snodec.gpg] %s/apt %s main\n' "$base" "$codename" > /etc/apt/sources.list.d/snodec.list
apt-get update
apt-get install snodec
```

The feed also supplies `mqttsuite`, `snodec-idp`, `core-ci`, `aisuite`, and `codexui` on their declared targets, with development components alongside runtime packages.

### Fedora and Rocky Linux

Choose `distribution=fedora` or `rocky`, the release number, and `x86_64` or `aarch64`.

```sh
base=https://raw.githubusercontent.com/SNodeC/Packages/feed
distribution=fedora
release=44
arch=x86_64
rpm --import "$base/keys/packages-gpg.asc"
printf '[snodec]\nname=SNode.C\nbaseurl=%s/rpm/%s/%s/%s\nenabled=1\ngpgcheck=1\nrepo_gpgcheck=1\ngpgkey=%s/keys/packages-gpg.asc\n' "$base" "$distribution" "$release" "$arch" "$base" > /etc/yum.repos.d/snodec.repo
dnf install snodec
```

### OpenWrt 24.10 (opkg)

Choose a catalog architecture from `opkg print-architecture`.

```sh
base=https://raw.githubusercontent.com/SNodeC/Packages/feed
arch=aarch64_cortex-a53
wget -O /etc/opkg/keys/bf7bc1c71f9755ef "$base/keys/openwrt-usign.pub"
printf 'src snodec %s/openwrt/24.10/%s\n' "$base" "$arch" >> /etc/opkg/customfeeds.conf
opkg update
opkg install snodec mqttsuite
```

### OpenWrt 25.12 (apk)

Choose a catalog architecture from `apk --print-arch`.

```sh
base=https://raw.githubusercontent.com/SNodeC/Packages/feed
arch=aarch64_cortex-a53
wget -O /etc/apk/keys/snodec.pub "$base/keys/openwrt-apk.pub"
printf '%s/openwrt/25.12/%s/packages.adb\n' "$base" "$arch" >> /etc/apk/repositories.d/customfeeds.list
apk update
apk add snodec mqttsuite
```

### Key fingerprints

| Key | Fingerprint |
| --- | --- |
| APT/RPM, RSA-4096 | `D1AAD199DB3D3E6A34B75E3047F6536301C7C67D` |
| usign | `bf7bc1c71f9755ef` |
| apk RSA-4096, SHA-256 of public DER | `c4ea1a8bb852973e4ad9528b645165fef7b208e4e1afbeea312ce38ec005a3c8` |

## Declarations and hooks

`ci/registry.json` lists participating repositories. `ci/targets.json` contains 40 targets. Each source's `ci/packages.json` declares its package, dependencies, targets, build dependencies, and hooks. Declarations and hooks come from the plan's exact source commit. Native hooks configure CMake Release under `/usr`, build with `--parallel $(nproc)`, run `ctest -LE mariadb --output-on-failure`, and invoke CPack. OpenWrt hooks use the product recipes inside one shared SDK per target.

| Environment | Meaning |
| --- | --- |
| `SOURCE_DIR` | Absolute source checkout at the plan commit |
| `OUTPUT_DIR` | Absolute directory for distribution packages |
| `TARGET_JSON` | JSON string containing the catalog entry and planned units |
| `PACKAGE_VERSION` | Upstream version from the plan |
| `PACKAGE_RELEASE` | Debian: revision plus `~codename`; RPM: revision plus `.disttag`; OpenWrt: numeric revision |
| `SDK_DIR` | Absolute shared OpenWrt SDK path; used by OpenWrt hooks |

Versions are `<version>-<revision>~<codename>` for DEB, `<version>-<revision>.<disttag>` for RPM, and `<version>-r<revision>` for OpenWrt. Each target artifact contains packages and a SHA-256 manifest. Publication verifies hashes, applies retention, rebuilds touched indices completely, signs packages/indices, and pushes one orphan-root commit. APT supports SHA-256 by-hash paths. `publications.json` records retained publications and binary package names for exact-version reuse.

The `publish` environment holds `PACKAGES_GPG_PRIVATE_KEY`, `OPENWRT_USIGN_PRIVATE_KEY`, and `OPENWRT_APK_PRIVATE_KEY`, and permits `main` only. `CORECI_CALLBACK_SECRET` is a repository secret. Empty `callback_url` disables callbacks; otherwise reports use core-ci's timestamp/body HMAC. Dispatch inputs are `wave_id`, `plan_url`, `callback_url`, `feed_branch`, and optional base64 `plan_json` for manual runs.

### CodexUI availability

CodexUI requires Qt6 Widgets/Network ≥ 6.6, KF6Sonnet ≥ 6.13, and libgit2 from standard repositories.

| Distribution | Releases | Architectures | Repository evidence |
| --- | --- | --- | --- |
| Debian | trixie, forky, sid | amd64, arm64, armhf, riscv64 | [Qt6](https://packages.debian.org/trixie/qt6-base-dev), [Sonnet](https://packages.debian.org/trixie/libkf6sonnet-dev), [libgit2](https://packages.debian.org/trixie/libgit2-dev) |
| Ubuntu | resolute | amd64, arm64 | [Qt6](https://packages.ubuntu.com/resolute/qt6-base-dev), [Sonnet](https://packages.ubuntu.com/resolute/libkf6sonnet-dev), [libgit2](https://packages.ubuntu.com/resolute/libgit2-dev) |
| Fedora | 43, 44 | x86_64, aarch64 | [Qt6](https://packages.fedoraproject.org/pkgs/qt6-qtbase/qt6-qtbase-devel/), [Sonnet](https://packages.fedoraproject.org/pkgs/kf6-sonnet/kf6-sonnet-devel/), [libgit2](https://packages.fedoraproject.org/pkgs/libgit2/libgit2-devel/) |
| Raspberry Pi OS | trixie | arm64 | Debian trixie base repositories |

Ubuntu noble and Raspberry Pi OS bookworm have older Qt/KF versions; Rocky's standard repositories lack the required KF6Sonnet. core-ci excludes Raspberry Pi OS bookworm's GCC-12 toolchain. Rocky 9 uses GCC Toolset 14. Raspberry Pi OS containers use Debian arm64 plus Raspberry Pi's signed archive.

armhf runs natively on `ubuntu-24.04-arm`: [runner qualification](https://github.com/SNodeC/Packages/actions/runs/43633330866) confirmed 32-bit execution with no QEMU handler. RISC-V uses Ubuntu's `qemu-user-static` on `ubuntu-24.04`. OpenWrt SDKs run on x86_64 using point releases 24.10.8 and 25.12.6. For OpenWrt reuse, the exact source recipe supplies SDK development headers, then the signed published runtime packages are installed into that staging tree.

## Verification

Run `python3 -m unittest discover -s tests -v`, `actionlint .github/workflows/wave.yml`, and `sh -n` for the source hooks. Tests cover version encoding, retention, APT Release golden fields, and the callback vector from core-ci main `649d0f8`. Staging qualification builds the canary targets and installs signed APT/RPM packages in fresh containers before implementation merges.
