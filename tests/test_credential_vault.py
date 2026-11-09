import pytest
from aitos.native.credential_vault import KeyringVault,VaultUnavailable
class Backend: priority=1
class FakeKeyring:
 class errors:
  class PasswordDeleteError(Exception): pass
 def __init__(self): self.backend=Backend(); self.values={}
 def get_keyring(self): return self.backend
 def set_password(self,s,u,p): self.values[(s,u)]=p
 def get_password(self,s,u): return self.values.get((s,u))
 def delete_password(self,s,u):
  try: del self.values[(s,u)]
  except KeyError: raise self.errors.PasswordDeleteError()
def fake_service():
 fake=FakeKeyring(); fake.backend.__class__.__module__="keyring.backends.SecretService"; return fake
def test_secret_service_roundtrip():
 fake=fake_service(); vault=KeyringVault(keyring_module=fake,platform="linux")
 vault.put("console/operator-password","synthetic-secret")
 assert vault.get("console/operator-password")=="synthetic-secret"
 assert vault.delete("console/operator-password") and not vault.delete("console/operator-password")
def test_plaintext_backend_rejected():
 fake=FakeKeyring(); fake.backend.__class__.__module__="keyring.backends.fail"
 with pytest.raises(VaultUnavailable): KeyringVault(keyring_module=fake,platform="linux")
def test_invalid_slot_and_secret():
 vault=KeyringVault(keyring_module=fake_service(),platform="linux")
 with pytest.raises(ValueError): vault.put("../secret","x")
 with pytest.raises(ValueError): vault.put("slot","x\0y")
