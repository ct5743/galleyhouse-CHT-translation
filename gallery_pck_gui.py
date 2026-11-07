#!/usr/bin/env python3
"""
Gallery PCK 修補工具（圖形介面）— 適用於 Windows，或任何已安裝 Python 與 tkinter 的系統。

流程（全部自動）：
  1. 備份所選的 .pck，並以 SHA-256 驗證備份副本。
  2. 將 .pck 解包到旁邊的 <名稱>_extracted/ 資料夾（逐檔 MD5 驗證）。
  3. 將替換包解壓縮到該資料夾，同名檔案會被取代，新檔案會被加入。
  4. 重新打包成新的 .pck，逐檔比對 MD5，確認無誤後才取代原始 .pck，
     最後刪除解包資料夾。

若在第 4 步完成前失敗，原始 .pck 不會被修改，解包資料夾也會保留以便檢查。
godot_pck.py 必須與本檔案放在同一資料夾。
"""
import argparse
import datetime
import hashlib
import io
import os
import queue
import shutil
import threading
import traceback
import zipfile
from contextlib import redirect_stdout
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk

import godot_pck as pck

DEFAULT_PAYLOAD = "gallery-zh-payload.zip"
CHUNK = 1 << 20


# ----------------------------------------------------------------------------
# 共用函式
# ----------------------------------------------------------------------------
def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for buf in iter(lambda: f.read(CHUNK), b""):
            h.update(buf)
    return h.hexdigest()


def md5_file(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for buf in iter(lambda: f.read(CHUNK), b""):
            h.update(buf)
    return h.digest()


def stamp():
    return datetime.datetime.now().strftime("%Y%m%d-%H%M%S")


class EngineOutput(io.TextIOBase):
    """攔截 godot_pck 的 print 輸出。只統計 MD5 錯誤，其餘訊息由本工具自行以中文記錄。"""

    def __init__(self):
        self.mismatches = 0
        self.buf = ""

    def write(self, s):
        self.buf += s
        while "\n" in self.buf:
            line, self.buf = self.buf.split("\n", 1)
            # 只計算逐檔錯誤行（"MD5 mismatch: 路徑"），不計算結尾的摘要行
            if line.startswith("MD5 mismatch:"):
                self.mismatches += 1
        return len(s)

    def flush(self):
        pass


# ----------------------------------------------------------------------------
# 處理流程（不含介面程式碼，可獨立測試）
# ----------------------------------------------------------------------------
def make_backup(src, log):
    dst = src.with_name(f"{src.name}.{stamp()}.bak")
    log(f"[1/4] 正在備份 {src.name} → {dst.name}")
    shutil.copy2(src, dst)
    if dst.stat().st_size != src.stat().st_size or sha256_file(src) != sha256_file(dst):
        raise RuntimeError("備份檢查失敗：副本與原檔不符。未做任何修改。")
    log(f"      備份已驗證（SHA-256 相符，{dst.stat().st_size:,} 位元組）。")
    return dst


def unpack(src, folder, file_count, log):
    log(f"[2/4] 正在解包到 {folder.name}\\")
    out = EngineOutput()
    args = argparse.Namespace(pck=str(src), output=str(folder), verify=True, quiet=True)
    with redirect_stdout(out):
        pck.cmd_unpack(args)
    if out.mismatches:
        raise RuntimeError(f"有 {out.mismatches} 個檔案在解包時 MD5 檢查失敗。")
    log(f"      解包完成：{file_count} 個檔案，MD5 全部相符。")


def apply_payload(zip_path, folder, log, choose=None):
    """將替換包套用到資料夾。回傳 [(相對路徑, md5)]，供最後驗證 .pck 內容使用。

    choose(名稱, 選項清單) 用於「替換包的翻譯檔在 .pck 內找不到同名檔案」時，
    讓使用者選擇要取代哪個現有翻譯檔；回傳 None 代表改為新增成獨立檔案。
    """
    log(f"[3/4] 正在將 {zip_path.name} 套用到 {folder.name}\\")
    replaced = added = 0
    applied = []
    with zipfile.ZipFile(zip_path) as z:
        entries = []
        for info in z.infolist():
            if info.is_dir():
                continue
            name = info.filename.replace("\\", "/")
            parts = [x for x in name.split("/") if x not in ("", ".")]
            if not parts:
                continue
            if name.startswith("/") or ".." in parts or ":" in parts[0]:
                raise RuntimeError(f"壓縮檔內含不安全的路徑：{info.filename}")
            entries.append((info, parts))
        if not entries:
            raise RuntimeError("替換包中沒有任何檔案。")

        # 若所有檔案都位於同一個包裝資料夾（例如 "gallery-zh-payload/"）內，
        # 且遊戲資料夾中尚無該名稱的資料夾，則移除該包裝層。
        tops = {p[0] for _, p in entries}
        strip = 0
        if len(tops) == 1 and all(len(p) > 1 for _, p in entries):
            top = next(iter(tops))
            if not (folder / top).is_dir():
                strip = 1
                log(f"      已移除壓縮檔內的包裝資料夾 '{top}/'。")

        # 現有檔案（不分大小寫）。Windows 檔案系統不分大小寫，但 .pck 內路徑分大小寫，
        # 因此一律沿用 .pck 內原本的大小寫寫入。
        existing = {}
        for p in folder.rglob("*"):
            if p.is_file() and p.name != pck.MANIFEST:
                rel = p.relative_to(folder).as_posix()
                existing[rel.lower()] = rel
        payload_rels = {"/".join(p[strip:]).lower() for _, p in entries}

        for info, parts in entries:
            rel = "/".join(parts[strip:])
            if rel.lower() in existing:
                rel = existing[rel.lower()]
            elif rel.lower().endswith(".translation"):
                # .pck 內沒有同名翻譯檔：列出現有翻譯檔，讓使用者決定。
                others = sorted(r for k, r in existing.items()
                                if k.endswith(".translation") and k not in payload_rels)
                log(f"      注意：.pck 內找不到「{rel}」。現有翻譯檔：")
                for o in others:
                    log(f"        - {o}")
                if not others:
                    log("        （沒有任何翻譯檔）")
                if others and choose is not None:
                    pick = choose(rel, others)
                    if pick:
                        log(f"      → 依照您的選擇，以替換包翻譯檔取代「{pick}」")
                        rel = pick
                    else:
                        log(f"      → 依照您的選擇，將「{rel}」新增成獨立檔案")
                else:
                    log(f"      → 將「{rel}」新增成獨立檔案（遊戲可能不會載入它）")
            target = folder.joinpath(*rel.split("/"))
            was_there = target.is_file()
            target.parent.mkdir(parents=True, exist_ok=True)
            h = hashlib.md5()
            with z.open(info) as src, open(target, "wb") as dst:
                for buf in iter(lambda: src.read(CHUNK), b""):
                    dst.write(buf)
                    h.update(buf)
            applied.append((rel, h.digest()))
            if was_there:
                replaced += 1
            else:
                added += 1
                log(f"      新增（非取代）：{rel}")
    log(f"      已取代 {replaced} 個檔案，新增 {added} 個檔案。")
    return applied


def repack(folder, out_path, log):
    log(f"[4/4] 正在將 {folder.name}\\ 重新打包成新的 .pck（保留原始格式）")
    out = EngineOutput()
    args = argparse.Namespace(directory=str(folder), output=str(out_path),
                              align=32, format=None, godot=None)
    with redirect_stdout(out):
        pck.cmd_repack(args)


def verify_pck(new_pck, folder, log):
    log("      正在比對新 .pck 與資料夾中的檔案……")
    expected = {}
    for p in folder.rglob("*"):
        if p.is_file() and p.name != pck.MANIFEST:
            expected[p.relative_to(folder).as_posix()] = md5_file(p)

    with open(new_pck, "rb") as f:
        info, entries = pck.read_pck(f)
        data = [e for e in entries if not e["flags"] & pck.FILE_REMOVAL]
        if len(data) != len(expected):
            raise RuntimeError(f"檔案數量不符：新 .pck 有 {len(data)} 個，"
                               f"資料夾有 {len(expected)} 個。")
        bad = []
        for e in data:
            rel = pck.disk_name(e["path"])
            if rel not in expected:
                bad.append(f"未預期的項目 {e['path']}")
                continue
            h = hashlib.md5()
            f.seek(e["offset"])
            left = e["size"]
            while left:
                buf = f.read(min(CHUNK, left))
                if not buf:
                    break
                h.update(buf)
                left -= len(buf)
            if left or h.digest() != expected[rel]:
                bad.append(e["path"])
    if bad:
        raise RuntimeError("驗證失敗：" + "、".join(bad[:5]))
    log(f"      通過：{len(data)} 個檔案，所有 MD5 皆相符。")
    return info


def verify_payload(new_pck, applied, log):
    """確認替換包的每個檔案都在新 .pck 內，且內容與替換包完全相同。"""
    with open(new_pck, "rb") as f:
        _, entries = pck.read_pck(f)
        by_name = {pck.disk_name(e["path"]): e for e in entries
                   if not e["flags"] & pck.FILE_REMOVAL}
        bad = []
        for rel, want in applied:
            e = by_name.get(rel)
            if e is None:
                bad.append(f"{rel}（.pck 內不存在）")
                continue
            h = hashlib.md5()
            f.seek(e["offset"])
            left = e["size"]
            while left:
                buf = f.read(min(CHUNK, left))
                if not buf:
                    break
                h.update(buf)
                left -= len(buf)
            if left or h.digest() != want:
                bad.append(f"{rel}（內容與替換包不符）")
    if bad:
        raise RuntimeError("替換包驗證失敗：" + "、".join(bad[:5]))
    others = [r for r, _ in applied if not r.lower().endswith(".ctex")]
    for r in others:
        log(f"      已確認 .pck 內的「{r}」與替換包內容相同。")
    log(f"      替換包共 {len(applied)} 個檔案，全部已確認寫入新 .pck。")


def run_pipeline(pck_path, payload_path, log, choose=None):
    if not pck_path.is_file():
        raise RuntimeError(f"找不到 .pck 檔案：{pck_path}")
    if not payload_path.is_file():
        raise RuntimeError(f"找不到替換包 zip：{payload_path}")

    with open(pck_path, "rb") as f:
        info, entries = pck.read_pck(f)
    encrypted = [e["path"] for e in entries if e["flags"] & pck.FILE_ENCRYPTED]
    if encrypted:
        raise RuntimeError(f"此 .pck 內含 {len(encrypted)} 個加密檔案，無法安全重新打包，"
                           "因此未做任何修改。")
    godot_ver = ".".join(map(str, info["godot"]))
    log(f"已找到：格式 v{info['format_version']}，Godot {godot_ver}，{len(entries)} 個項目")

    backup = make_backup(pck_path, log)

    folder = pck_path.with_name(pck_path.stem + "_extracted")
    if folder.exists():
        old = folder.with_name(f"{folder.name}.old-{stamp()}")
        log(f"      既有資料夾已改名為 {old.name}（未刪除）")
        folder.rename(old)

    unpack(pck_path, folder, len(entries), log)
    applied = apply_payload(payload_path, folder, log, choose)

    tmp = pck_path.with_name(pck_path.name + ".new")
    try:
        repack(folder, tmp, log)
        verify_pck(tmp, folder, log)
        verify_payload(tmp, applied, log)
        os.replace(tmp, pck_path)
    except BaseException:
        if tmp.exists():
            tmp.unlink()
        raise

    # 成功後刪除解包資料夾。若刪除失敗，只提示，不影響結果。
    try:
        shutil.rmtree(folder)
        cleanup = f"已刪除解包資料夾 {folder.name}"
    except OSError as e:
        cleanup = f"無法刪除解包資料夾 {folder.name}，請手動刪除（{e}）"

    log("")
    log(f"完成！{pck_path.name} 已被替換。")
    log(f"原始檔備份：{backup.name}")
    log(cleanup)


# ----------------------------------------------------------------------------
# 圖形介面
# ----------------------------------------------------------------------------
class App:
    def __init__(self, root):
        self.root = root
        self.q = queue.Queue()
        self.running = False
        root.title("Gallery PCK 修補工具")
        root.geometry("780x560")

        frm = ttk.Frame(root, padding=10)
        frm.pack(fill="both", expand=True)
        frm.columnconfigure(1, weight=1)

        self.pck_var = tk.StringVar()
        self.zip_var = tk.StringVar()

        ttk.Label(frm, text="遊戲 .pck 檔案：").grid(row=0, column=0, sticky="w")
        ttk.Entry(frm, textvariable=self.pck_var).grid(row=0, column=1, sticky="ew", padx=6)
        ttk.Button(frm, text="瀏覽…", command=self.pick_pck).grid(row=0, column=2)

        ttk.Label(frm, text="替換包 .zip：").grid(row=1, column=0, sticky="w", pady=4)
        ttk.Entry(frm, textvariable=self.zip_var).grid(row=1, column=1, sticky="ew", padx=6, pady=4)
        ttk.Button(frm, text="瀏覽…", command=self.pick_zip).grid(row=1, column=2, pady=4)

        self.start_btn = ttk.Button(frm, text="開始（備份、解包、替換、重新打包）",
                                    command=self.start)
        self.start_btn.grid(row=2, column=0, columnspan=3, sticky="w", pady=8)

        self.progress = ttk.Progressbar(frm, mode="indeterminate")
        self.progress.grid(row=3, column=0, columnspan=3, sticky="ew")

        self.log_box = scrolledtext.ScrolledText(frm, height=20, state="disabled",
                                                 font=("Microsoft JhengHei", 10))
        self.log_box.grid(row=4, column=0, columnspan=3, sticky="nsew", pady=(8, 0))
        frm.rowconfigure(4, weight=1)

        self._append("請選擇 galleyhouse.pck 與替換包 zip，然後按「開始」。")
        self._append("開始前會先備份 .pck，備份完成後才會進行任何修改。")
        root.after(100, self.poll)

    # --- 選檔 ---------------------------------------------------------------
    def pick_pck(self):
        p = filedialog.askopenfilename(title="選擇 .pck 檔案",
                                       filetypes=[("Godot PCK", "*.pck"), ("所有檔案", "*.*")])
        if p:
            self.pck_var.set(p)
            guess = Path(p).with_name(DEFAULT_PAYLOAD)
            if guess.is_file() and not self.zip_var.get():
                self.zip_var.set(str(guess))

    def pick_zip(self):
        p = filedialog.askopenfilename(title="選擇替換包 zip",
                                       filetypes=[("Zip 壓縮檔", "*.zip"), ("所有檔案", "*.*")])
        if p:
            self.zip_var.set(p)

    # --- 執行 ---------------------------------------------------------------
    def start(self):
        if self.running:
            return
        pck_path = Path(self.pck_var.get().strip().strip('"'))
        zip_path = Path(self.zip_var.get().strip().strip('"'))
        if not pck_path.is_file():
            messagebox.showerror("找不到檔案", "請先選擇有效的 .pck 檔案。")
            return
        if not zip_path.is_file():
            messagebox.showerror("找不到檔案", "請先選擇有效的替換包 .zip 檔案。")
            return
        if not messagebox.askyesno(
                "確認",
                f"即將替換：\n{pck_path}\n\n會先建立已驗證的備份。\n\n是否繼續？"):
            return

        self.running = True
        self.start_btn.config(state="disabled")
        self.progress.start(12)
        self._append("=" * 70)
        threading.Thread(target=self._worker, args=(pck_path, zip_path), daemon=True).start()

    def _worker(self, pck_path, zip_path):
        try:
            run_pipeline(pck_path, zip_path, self.log, self.ask_translation)
            self.q.put(("ok", ""))
        except SystemExit as e:
            self.q.put(("err", str(e)))
        except Exception as e:
            self.log(traceback.format_exc())
            self.q.put(("err", str(e)))

    # --- 讓使用者選擇要取代的翻譯檔 ---------------------------------------------
    def ask_translation(self, name, options):
        """在背景執行緒呼叫：請主執行緒顯示對話框並等待選擇。"""
        ev = threading.Event()
        holder = {}
        self.q.put(("ask", (name, options, ev, holder)))
        ev.wait()
        return holder.get("choice")

    def _show_choice(self, name, options, ev, holder):
        top = tk.Toplevel(self.root)
        top.title("選擇要取代的翻譯檔")
        top.transient(self.root)
        top.grab_set()
        ttk.Label(top, justify="left",
                  text=f"遊戲 .pck 內沒有「{name}」。\n"
                       "請選擇要用替換包的翻譯檔取代哪一個現有翻譯檔：").pack(
            padx=12, pady=(12, 6), anchor="w")
        lb = tk.Listbox(top, width=70, height=min(10, len(options) + 1),
                        exportselection=False)
        for o in options:
            lb.insert("end", o)
        lb.insert("end", "（不取代，改為新增成獨立檔案）")
        guess = next((i for i, o in enumerate(options) if "zh" in o.lower()), 0)
        lb.selection_set(guess)
        lb.pack(padx=12, pady=6)

        def done(ok):
            sel = lb.curselection()
            holder["choice"] = options[sel[0]] if ok and sel and sel[0] < len(options) else None
            top.destroy()
            ev.set()

        row = ttk.Frame(top)
        row.pack(pady=(4, 12))
        ttk.Button(row, text="確定", command=lambda: done(True)).pack(side="left", padx=6)
        ttk.Button(row, text="新增為獨立檔案", command=lambda: done(False)).pack(side="left", padx=6)
        top.protocol("WM_DELETE_WINDOW", lambda: done(False))

    # --- 執行緒安全的記錄 -----------------------------------------------------
    def log(self, msg):
        self.q.put(("log", msg))

    def _append(self, msg):
        self.log_box.config(state="normal")
        self.log_box.insert("end", msg + "\n")
        self.log_box.see("end")
        self.log_box.config(state="disabled")

    def _finish(self):
        self.running = False
        self.progress.stop()
        self.start_btn.config(state="normal")

    def poll(self):
        try:
            while True:
                kind, msg = self.q.get_nowait()
                if kind == "log":
                    self._append(msg)
                elif kind == "ask":
                    self._show_choice(*msg)
                elif kind == "ok":
                    self._finish()
                    messagebox.showinfo("完成", "galleyhouse.pck 已重新打包並通過驗證。\n\n"
                                                "備份檔位於相同資料夾。")
                elif kind == "err":
                    self._finish()
                    self._append("失敗：" + msg)
                    messagebox.showerror("失敗", "發生錯誤，原始 .pck 未被替換。\n\n"
                                                 + msg.splitlines()[0])
        except queue.Empty:
            pass
        self.root.after(100, self.poll)


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
