import json
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skills" / "product-hunter" / "scripts" / "price.py"


def run(*args) -> dict:
    out = subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


def test_laptop_deal():
    r = run("laptop", "Lenovo ThinkPad T14 Gen 2 i5-1135G7 16GB 512GB", "--desc", "funktioniert, mit Netzteil", "--ask", "180")
    assert r["features"]["year"] == 2021
    assert r["deal"]["discount_vs_typical"] > 0.3
    assert r["deal"]["exits"][r["deal"]["best_exit"]]["verdict"] == "BUY"


def test_unknown_device_points_to_generic():
    assert "error" in run("laptop", "Samsung Galaxy S24")


def test_generic_and_flip():
    g = run("generic", "--new-price", "1000", "--age", "1", "--category", "phone", "--ask", "500")
    assert g["estimates_eur"]["ebay_sold"] == 700 and g["deal"]["discount_vs_typical"] > 0.3
    f = run("flip", "--buy", "90", "--sell", "265", "--sell-on", "ebay", "--hours", "1.5")
    assert f["max_buy_for_30_ev"] > 90 and f["verdict"] == "BUY"


def test_build_bundle(tmp_path):
    subprocess.run([sys.executable, str(ROOT / "scripts" / "build_skill.py")], check=True, capture_output=True)
    z = zipfile.ZipFile(ROOT / "dist" / "product-hunter.zip")
    names = set(z.namelist())
    assert {"product-hunter/SKILL.md", "product-hunter/scripts/price.py",
            "product-hunter/scripts/huntbot_core/pricemodel.py"} <= names
    z.extractall(tmp_path)
    out = subprocess.run([sys.executable, "-I", str(tmp_path / "product-hunter/scripts/price.py"), "laptop", "ThinkPad T480 8GB"],
                         capture_output=True, text=True, check=True)
    assert json.loads(out.stdout)["features"]["line"] == "thinkpad_t"
