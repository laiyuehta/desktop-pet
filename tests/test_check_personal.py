import importlib.util
import os
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("check_personal", os.path.join(HERE, "..", "tools", "ci", "check_personal.py"))
cp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cp)


class CheckPersonalTest(unittest.TestCase):
    def test_hash_match_is_case_insensitive_and_bounded(self):
        hashes = cp.load_hashes(f"# demo\n10:{cp.digest('MyHome-123')}\n")
        self.assertEqual(cp.hash_hits("連到 myhome-123 了", hashes), 1)
        self.assertEqual(cp.hash_hits("ssid=MYHOME-123\n", hashes), 1)
        self.assertEqual(cp.hash_hits("xmyhome-123", hashes), 0)
        self.assertEqual(cp.hash_hits("myhome-1234", hashes), 0)
        self.assertEqual(cp.hash_hits("PetRescue-AP", hashes), 0)

    def test_personal_rules_from_gitleaks_config(self):
        rules = cp.personal_rules()
        ids = {r[0] for r in rules}
        self.assertIn("personal-private-ipv4", ids)
        self.assertIn("personal-ddns-domain", ids)
        # 範例字串拆開寫，免得 gitleaks 掃 repo 時把測試本身當成洩漏
        ddns = "mypet." + "ddns.net"
        lan = "192." + "168.1.20"
        hits = cp.rule_hits(f"後端在 https://{ddns}，Pi 在 {lan}，熱點 10.42.0.1，文件 192.0.2.10", rules)
        self.assertEqual(sorted(hits), ["personal-ddns-domain", "personal-private-ipv4"])
        self.assertEqual(cp.rule_hits(r"C:\Users\<user>\desktop-pet、127.0.0.1、pet.example.com", rules), [])
        self.assertEqual(cp.rule_hits("C:\\" + "Users\\alice\\desktop-pet", rules), ["personal-windows-user-path"])


if __name__ == "__main__":
    unittest.main()
