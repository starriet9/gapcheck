from pathlib import Path

import pytest

from gapcheck import load, patches

FIXTURE = Path(__file__).parent / "fixtures" / "synthea_mini"
SEED = 20261120  # dev seed; the fixture patients come from this run

# Fixture patients (dev run seed 20261120).
DECEASED = "11eca386-d055-b951-21d4-6f4746cdedde"
UNDER_30 = "77afbd26-13f5-5d1f-f991-2d66f968a50c"
P1_ONLY = "83a82e83-79b2-273a-d14e-e878cb9be324"      # no eGFR at all, no kidney dx
DIABETES = "8bea4be0-9b19-7879-3fcb-7222f50872b6"     # T2DM, native eGFR > 150
ASCVD = "92f25043-4ddc-f67a-e7eb-f1e14cf55030"        # IHD + PCI, on simvastatin
CKD3 = "9bc5450a-775d-7fdd-6db8-904796677040"         # CKD 1-3 active, both eGFR series
STROKE = "9e975b2d-8047-d369-7c26-25aaeff13c9b"       # stroke only, no statin
S2 = "ab9413fb-08e6-6646-5aa1-b17822087b70"           # LDL >= 190 now and before; mL/min eGFR only
FEMALE = "b28fc335-5ebb-bb39-67c1-a482dc15e0cf"       # one lipid panel, LDL 229.6; past pregnancy
ALLERGY = "bd88961c-eb9f-72f8-b3f8-f7551ca5c653"      # aspirin allergy


@pytest.fixture
def loaded(tmp_path):
    con = load.load(FIXTURE, tmp_path / "t.duckdb", seed=SEED)
    yield con
    con.close()


@pytest.fixture
def patched(tmp_path):
    con = load.load(FIXTURE, tmp_path / "p.duckdb", seed=SEED)
    patches.apply_all(con, SEED)
    yield con
    con.close()
