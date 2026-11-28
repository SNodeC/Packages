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

The target catalog assigns runner labels, request prefixes, and runner architectures. The build workflow uses an allowlist generated from that catalog, and the build checks the runner against the signed target. It installs the complete
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
pins from `ci/targets.json`. Family dependency lists can be empty or omitted. Selectors such as `rpm@rocky-9` and `rpm@rocky-9-aarch64` add dependencies for matching targets. A range such as `>=3.0,<4` requires both conditions.

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

OpenWrt targets require both `build.openwrt` and `build.openwrt_register`. The registration hook is a path under the repository’s `ci/` directory. The workflow registers every package in the frozen closure before resolving feeds, then stages development files in one SDK in dependency order. The catalog’s `apk_signing_sdk` names the SDK target that supplies the publisher’s signing tool.

## New repository

- [ ] Add the repository to the allowlist in `ci/registry.json`.
- [ ] Add `ci/packages.json` with schema 2, target IDs, and any dependency ranges.
- [ ] Declare the hooks for each target family. The scheduler marks targets with missing hooks as unsupported.
- [ ] Add repository-owned package recipes and metadata under `ci/`.
- [ ] Set the `releases` range to the stable versions those hooks can build.
- [ ] Push the first stable tag in the supported range.

The catalog defines the target environments. When runner routes change, regenerate the workflow with `python3 ci/generate_workflow_runners.py`.

Raspberry Pi OS targets use Debian arm64 containers and Raspberry Pi's signed archive. The catalog also specifies whether each target uses native execution or emulation. Native armhf execution was verified on the ARM runner. [Runner qualification](https://github.com/SNodeC/Packages/actions/runs/43633330866)

## Verification

Run `python3 -m unittest discover -s tests -v` and
`actionlint .github/workflows/*.yml`. For native OpenWrt index signing, run
`python3 tests/openwrt_signing.py SDK_DIR` with the catalog's APK SDK.
The workflow tests use local fixture repositories and disposable signing keys.
