# SNode.C packages

This repository builds and signs distribution packages. The registry and target catalog
describe the participating repositories and target environments. core-ci owns scheduling
and sends one immutable request per build and one immutable plan per publication.
Source repositories own their declarations, hooks, recipes, and package metadata.
The production feed is `feed`. Qualification uses `feed-staging`.

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

## Contract X workflows

| Workflow | Dispatch inputs |
| --- | --- |
| `build.yml` | `request_id`, `attempt`, `feed_branch` |
| `publish.yml` | `publication_id`, `attempt`, `feed_branch`, staging-only `fault` |
| `restore.yml` | `feed_branch`, `snapshot_ref`, `expected_head` |

Build and publication derive the scheduler URL from `vars.CORECI_ORIGIN`.
The claim verifies ownership for the attempt and authenticates the immutable
request or plan by SHA-256. A rejected claim with status 409 ends the workflow
successfully without building or publishing. The publisher verifies the plan
again immediately before the push.

Each build runs on the hosted platform selected by core-ci from the frozen
target catalog. Requests beginning with `arm.` select the native ARM runner.
The build verifies that runner against the signed target, installs the complete
bound inventory from `feed_commit`, and checks the installed native identities.
Only the requested item's packages are uploaded. The artifact remains available
for 90 days, subject to GitHub's repository retention setting.

Publication validates the downloaded archive and each split package, quarantines
invalid items, and publishes valid items in a single orphan commit. Its atomic
push checks the feed head lease, creates the publication snapshot, and applies
the approved snapshot deletions. Receipts in `publications.json` allow core-ci
to recover after a lost callback or an interruption after the push. The oldest
rollback point remains protected while it is among the three most recent
reconciled publications. Restore updates only the feed branch, with a lease.

Contract X introduces per-distribution APT paths. After cutover each new
source line uses `$base/apt/$distribution $codename main`, for example
`$base/apt/debian trixie main`. The earlier source line shown under Installation
applies to the pre-cutover feed and the 30-day legacy retention period.

## Declarations and hooks

The schema-2 declaration at `ci_commit` specifies the supported release range,
targets, hooks, direct `depends_on` version ranges, and build dependencies.
Targets derive system repository setup, base images, and OpenWrt SDK and feed
pins from `ci/targets.json`. A selector such as `rpm@rocky-9` adds dependencies
only on matching targets. A range such as `>=3.0,<4` combines its conditions with AND.

| Hook environment | Meaning |
| --- | --- |
| `SOURCE_DIR` | Tagged source checkout, with nested submodules |
| `CI_DIR` | Repository's `ci/` tree at the frozen CI commit |
| `DEPENDENCIES_JSON` | Resolved direct dependencies with package, version, and revision |
| `OUTPUT_DIR` | Directory for the hook's split binary packages |
| `TARGET_JSON` | Frozen target catalog entry |
| `PACKAGE_VERSION` | Upstream version |
| `PACKAGE_RELEASE` | Revision with the native packaging suffix, or the numeric OpenWrt revision |
| `SDK_DIR` | Shared build SDK for OpenWrt |

OpenWrt declarations provide a recipe registration hook at
`ci/package/openwrt-register.sh`. The workflow registers the entire closure
before resolving feeds, then builds each bound output in dependency order so
`Build/InstallDev` stages the required development files in one SDK.

### CodexUI availability

CodexUI requires Qt6 Widgets/Network ≥ 6.6, KF6Sonnet ≥ 6.13, and libgit2 from standard repositories.

Decision: Qt desktop application; 13 functional test failures under riscv64 emulation, tracked as [CodexUI issue #121](https://github.com/SNodeC/CodexUI/issues/121). Debian riscv64 is excluded from CodexUI's package declaration.

| Distribution | Releases | Architectures | Repository evidence |
| --- | --- | --- | --- |
| Debian | trixie, forky, sid | amd64, arm64, armhf | [Qt6](https://packages.debian.org/trixie/qt6-base-dev), [Sonnet](https://packages.debian.org/trixie/libkf6sonnet-dev), [libgit2](https://packages.debian.org/trixie/libgit2-dev) |
| Ubuntu | resolute | amd64, arm64 | [Qt6](https://packages.ubuntu.com/resolute/qt6-base-dev), [Sonnet](https://packages.ubuntu.com/resolute/libkf6sonnet-dev), [libgit2](https://packages.ubuntu.com/resolute/libgit2-dev) |
| Fedora | 43, 44 | x86_64, aarch64 | [Qt6](https://packages.fedoraproject.org/pkgs/qt6-qtbase/qt6-qtbase-devel/), [Sonnet](https://packages.fedoraproject.org/pkgs/kf6-sonnet/kf6-sonnet-devel/), [libgit2](https://packages.fedoraproject.org/pkgs/libgit2/libgit2-devel/) |
| Raspberry Pi OS | trixie | arm64 | Debian trixie base repositories |

Ubuntu noble and Raspberry Pi OS bookworm have older Qt/KF versions; Rocky's standard repositories lack the required KF6Sonnet. core-ci excludes Raspberry Pi OS bookworm's GCC-12 toolchain. Rocky 9 uses GCC Toolset 14. Raspberry Pi OS containers use Debian arm64 plus Raspberry Pi's signed archive.

armhf runs natively on `ubuntu-24.04-arm`: [runner qualification](https://github.com/SNodeC/Packages/actions/runs/43633330866) confirmed 32-bit execution with no QEMU handler. RISC-V uses Ubuntu's `qemu-user-static` on `ubuntu-24.04`. Contract X builds one package and target per job, including on emulated architectures. OpenWrt SDKs run on x86_64 using point releases 24.10.8 and 25.12.6. For OpenWrt reuse, the exact source recipe supplies SDK development headers, then the signed published runtime packages are installed into that staging tree.

## Verification

Run `python3 -m unittest discover -s tests -v` and
`actionlint .github/workflows/*.yml`. For native OpenWrt index signing, run
`python3 tests/openwrt_signing.py SDK_DIR` with the catalog's APK SDK.
The workflow tests use local fixture repositories and disposable signing keys.
