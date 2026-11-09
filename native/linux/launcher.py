"""Linux manager for the authenticated read-only operator console."""
from __future__ import annotations
import asyncio,secrets,sys,webbrowser
from pathlib import Path
import tkinter as tk
from tkinter import messagebox,simpledialog
from aitos.console_gateway import is_valid_operator_password
from aitos.native.console_host import LocalConsoleHost,exercise_local_host
from aitos.native.credential_vault import get_credential_vault
def _assets_dir(): return Path(getattr(sys,"_MEIPASS",Path(__file__).resolve().parents[2]))/"console_dist"
def main():
 root=tk.Tk(); root.title("AITOS — local read-only console"); root.geometry("480x220")
 tk.Label(root,text="Linux Secret Service · authenticated read-only console",wraplength=440).pack(pady=18)
 try:
  vault=get_credential_vault(); password=vault.get("console/operator-password")
  if password is None:
   password=simpledialog.askstring("AITOS setup","Choose operator password (24–1024 UTF-8 bytes)",show="*",parent=root)
   if password is None: root.destroy(); return
   confirmation=simpledialog.askstring("Confirm","Repeat operator password",show="*",parent=root)
   if password!=confirmation or not is_valid_operator_password(password): raise ValueError("password policy")
   vault.put("console/operator-password",password)
  port=simpledialog.askinteger("Backend connection","Existing local backend port (or SSH-forwarded port)",initialvalue=8090,minvalue=1,maxvalue=65535,parent=root)
  if port is None: root.destroy(); return
  slot=f"console/backend-token/{port}"; token=vault.get(slot)
  if token is None:
   token=simpledialog.askstring("Backend authentication","Backend bearer token (at least 32 characters)",show="*",parent=root)
   if token is None: root.destroy(); return
   if len(token)<32 or "\r" in token or "\n" in token: raise ValueError("invalid token")
   vault.put(slot,token)
 except Exception:
  messagebox.showerror("AITOS","OS credential vault/setup failed. No server started; plaintext fallback is forbidden.",parent=root); root.destroy(); return
 dist=_assets_dir()
 if not dist.is_dir(): messagebox.showerror("AITOS","Packaged console assets missing.",parent=root); root.destroy(); return
 host=LocalConsoleHost(dist,upstream_port=port,password=password,token=token); host.start()
 def close():
  if not host.stop(): messagebox.showerror("AITOS","Shutdown is still draining; retry.",parent=root); return
  root.destroy()
 def opened():
  if not host.ready.is_set(): root.after(100,opened); return
  if host.failed: messagebox.showerror("AITOS","Local gateway could not start.",parent=root); return
  tk.Label(root,text="Read-only console ready. Closing manager revokes sessions.",wraplength=440).pack(pady=8)
  tk.Button(root,text="Open authenticated console",command=lambda:webbrowser.open(host.origin)).pack(pady=6); webbrowser.open(host.origin)
 root.protocol("WM_DELETE_WINDOW",close); tk.Button(root,text="Close console and revoke sessions",command=close).pack(pady=8)
 root.after(100,opened); root.mainloop()
def self_test():
 if get_credential_vault() is None: raise RuntimeError("credential vault unavailable")
 dist=_assets_dir()
 if not dist.is_dir(): raise RuntimeError("console assets missing")
 from aitos.console_gateway import GatewayConfig,create_gateway
 create_gateway(GatewayConfig("http://127.0.0.1:8780","http://127.0.0.1:8090",dist),password=secrets.token_urlsafe(32))
if __name__=="__main__":
 if sys.argv[1:] in (["--self-test"],["--gateway-self-test"]):
  try:
   self_test()
   if sys.argv[1:]==["--gateway-self-test"]: asyncio.run(exercise_local_host(_assets_dir()))
  except Exception: raise SystemExit(2) from None
 elif sys.argv[1:]: raise SystemExit(2)
 else: main()
