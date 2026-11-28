# APT sources after the feed cutover

The old `apt` index and its pool are frozen for 30 days. To receive updates,
replace `$base/apt` in the APT sources line with `$base/apt/$distribution`.
Use `debian`, `ubuntu`, or `raspberrypios` as appropriate. Keep the same
codename, component, and signing key. The new Debian trixie line is:

```text
deb [signed-by=/usr/share/keyrings/snodec.gpg] https://raw.githubusercontent.com/SNodeC/Packages/feed/apt/debian trixie main
```

If the feed is restored to the pre-X snapshot, switch back to the earlier
source line using `$base/apt`.
