from .context import Alliant
from .schema import Category, ParsedMessage, ServiceFlag
from .service_codes import review
from .test_garments import write


def parsed(acct=None, **flag):
    return ParsedMessage(category=Category.service_code, account_number=acct, summary="s",
                         service=ServiceFlag(**{"scope": "garment", "kind": "repair", "reason": "torn pocket", **flag}))


def test_one_garment_by_barcode_finds_the_account(tmp_path):
    write(tmp_path)
    a = Alliant.from_dir(str(tmp_path))
    p = parsed(barcodes=["100001"])
    lines, rb, qs = review(p, a)
    assert p.account_number == "1205-1-00007" and qs == []
    assert lines[0] == "🧵 *Repair (service code)*" and lines[1] == "*1205-1-00007*  MIDAS 7"
    assert "100001 SHT NAVY L/S L" in lines[2] and "wearer #12 Joey Smith" in lines[2]
    assert "Reason: torn pocket" in lines and any('"Add Service Code"' in l for l in lines)
    assert rb == "✅ Service code on garment 100001 for repair (torn pocket): the plant will pull it at the next scan."


def test_wearer_audit_and_unknown_barcode(tmp_path):
    write(tmp_path)
    a = Alliant.from_dir(str(tmp_path))
    lines, rb, qs = review(parsed("1205-1-00007", scope="wearer", kind="inspection", reason="audit", wearer="Joey"), a)
    assert "• Wearer *#12 Joey Smith*: all 2 garments in service" in lines and qs == []
    assert any('"Specific Wearers"' in l for l in lines) and "Joey Smith's garments for inspection / audit" in rb
    lines, _, qs = review(parsed("1205-1-00007", barcodes=["555"]), a)
    assert any("isn't in Alliant's latest data" in l for l in lines)
    _, _, qs = review(parsed("1205-1-00007"), a)
    assert qs and qs[0].startswith("Which garment?")


def test_inventory_at_an_account(tmp_path):
    write(tmp_path)
    a = Alliant.from_dir(str(tmp_path))
    lines, rb, qs = review(parsed("1205-1-00007", scope="customer", kind="inventory", reason="count all mats"), a)
    assert lines[0] == "📋 *Inventory count requested*" and qs == []
    assert rb == "✅ Inventory count scheduled for MIDAS 7 (count all mats)."
    _, _, qs = review(parsed(None, scope="customer", kind="inventory", reason="x"), a)
    assert qs == ["Which customer?"]
