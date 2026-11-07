# 加利宅邸懸案 繁體中文化

修補工具：選擇遊戲的 `galleyhouse.pck`，自動備份、解包、套用替換包、重新打包。

- 下載：[Releases](../../releases/latest)（`GalleryPckPatcher.exe` 與 `gallery-zh-payload.zip`，放在同一個資料夾）
- 說明頁：啟用 GitHub Pages 後，見 `docs/index.html`

## 從原始碼執行
需要 Python 3 與 tkinter：`python gallery_pck_gui.py`
自行建置 exe：在 Windows 雙擊 `build_windows.bat`。

## 發佈新版本
`git tag v0.1 && git push origin v0.1`，GitHub Actions 會建置 exe 並附加到該版本的 Release。
替換包 `gallery-zh-payload.zip` 檔案較大，請手動上傳到同一個 Release。
