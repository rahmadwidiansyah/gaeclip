# Icons (required before first Windows build)

`tauri.conf.json` references `icons/icon.ico`. Generate it with the
Tauri CLI on any machine that has the frontend assets:

```sh
npx @tauri-apps/cli icon frontend/public/icon.png  # or any 1024px source
```

then copy the produced `icon.ico` here. The NSIS bundler fails without it.
