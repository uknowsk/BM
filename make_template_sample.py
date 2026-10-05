"""Sample classification forms (분류양식) for the Gauge template feature.

    python make_template_sample.py        # writes data/template_sample.xlsx (cooking) and data/template_sample_refrigerator.xlsx

Columns: 구분 | 항목(KO) | Item(EN) | 동의어/Synonyms | 단위/Unit | 유형/Type | 비고/Notes. The sheet name is the product group hint
('조리기기' / '냉장고'). Category cells are merged per category (the parser carries the last non-empty 구분 forward anyway).
"""
import io
import sys
from pathlib import Path

from openpyxl import Workbook
from openpyxl.comments import Comment
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation

ROOT = Path(__file__).resolve().parent
HEADER = ["구분", "항목(KO)", "Item(EN)", "동의어/Synonyms", "단위/Unit", "유형/Type", "비고/Notes"]
NUM, FLAG, TEXT, LIST = "숫자", "예/아니오", "텍스트", "목록"
FILES = {"cooking": "template_sample.xlsx", "refrigerator": "template_sample_refrigerator.xlsx"}
SHEETS = {"cooking": "조리기기", "refrigerator": "냉장고"}

# (category, [(KO, EN, synonyms, unit, type, notes)])
COOKING = [
    ("치수·무게", [
        ("폭", "Width", "Overall Width, Cabinet Width", "mm", NUM, "제품 외곽 폭"),
        ("높이", "Height", "Overall Height", "mm", NUM, ""),
        ("깊이", "Depth", "Overall Depth", "mm", NUM, "손잡이 제외 여부는 원문 확인"),
        ("설치 컷아웃 폭", "Cutout width", "Cutout Dimensions Width", "mm", NUM, "빌트인 설치 치수"),
        ("설치 컷아웃 높이", "Cutout height", "", "mm", NUM, ""),
        ("설치 컷아웃 깊이", "Cutout depth", "", "mm", NUM, ""),
        ("제품 무게", "Weight", "Net Weight", "kg", NUM, ""),
        ("도어 열림 시 깊이", "Depth with door open", "", "mm", NUM, ""),
    ]),
    ("전기·에너지", [
        ("정격 전압", "Voltage", "Volts", "V", NUM, ""),
        ("정격 전류", "Amps", "Amp Rating", "A", NUM, ""),
        ("주파수", "Frequency", "Hertz", "Hz", NUM, ""),
        ("베이크 소비전력", "Bake wattage", "Bake Element Power", "W", NUM, "상단/하단이 나뉘면 검토 필요"),
        ("컨벡션 소비전력", "Convection wattage", "Convection Element Power", "W", NUM, ""),
        ("마이크로웨이브 출력", "Microwave power", "Cooking Power, Microwave cooking power", "W", NUM, "SCO/콤비 오븐"),
        ("연간 에너지 소비량", "Annual energy use", "Energy Rating", "kWh/yr", NUM, ""),
        ("에너지스타 인증", "ENERGY STAR", "에너지스타", "", FLAG, ""),
    ]),
    ("조리 기능", [
        ("오븐 용량", "Oven capacity", "Total Capacity", "L", NUM, "cu ft는 L로 변환됨"),
        ("컨벡션", "Convection", "True Convection", "", FLAG, ""),
        ("컨벡션 베이크", "Convection Bake", "Convect Bake", "", FLAG, ""),
        ("컨벡션 로스트", "Convection Roast", "", "", FLAG, ""),
        ("에어프라이", "Air fry", "No Preheat Air Fry, Air Fry", "", FLAG, "제조사마다 표기가 다름"),
        ("브로일", "Broil", "", "", FLAG, ""),
        ("마이크로웨이브", "Microwave", "", "", FLAG, ""),
        ("온도 프로브", "Temperature probe", "Meat Probe", "", FLAG, ""),
        ("보온", "Keep warm", "Warm", "", FLAG, ""),
        ("발효", "Proof", "Bread Proof", "", FLAG, ""),
        ("스팀 조리", "Steam cooking", "Steam Bake", "", FLAG, ""),
        ("조리 모드 목록", "Oven cooking modes", "Cooking Modes, Oven Selections", "", LIST, "원문 모드명을 그대로 나열"),
    ]),
    ("편의·안전", [
        ("자가 세척", "Self clean", "Self-Cleaning", "", FLAG, ""),
        ("스팀 세척", "Steam clean", "", "", FLAG, ""),
        ("안식일 모드", "Sabbath mode", "", "", FLAG, ""),
        ("도어 잠금", "Door lock", "Control Lock", "", FLAG, ""),
        ("랙 개수", "Number of oven racks", "Rack count", "개", NUM, "목록에서 계산될 수 있음(derived)"),
        ("오븐 조명 개수", "Number of oven lights", "", "개", NUM, ""),
        ("자동 전원 차단", "Auto shut-off", "Oven Automatic Shut-Off", "", FLAG, ""),
        ("컨트롤 방식", "Control type", "", "", TEXT, ""),
    ]),
    ("연결성", [
        ("Wi-Fi 연결", "Wi-Fi", "WiFi Connect, Smart Appliance", "", FLAG, ""),
        ("음성 제어", "Voice control", "Alexa, Google Assistant", "", FLAG, ""),
        ("원격 제어", "Remote control", "Remote Oven Control", "", FLAG, ""),
        ("연동 앱/서비스", "Connected services", "Works With", "", TEXT, ""),
    ]),
]

REFRIGERATOR = [
    ("치수·무게", [
        ("폭", "Width", "Overall Width", "mm", NUM, ""),
        ("높이", "Height", "Overall Height", "mm", NUM, ""),
        ("깊이", "Depth", "Overall Depth", "mm", NUM, ""),
        ("제품 무게", "Weight", "Net Weight", "kg", NUM, ""),
    ]),
    ("용량·에너지", [
        ("총 용량", "Total capacity", "Total Volume", "L", NUM, "cu ft는 L로 변환됨"),
        ("냉장실 용량", "Fridge capacity", "Fresh Food Capacity", "L", NUM, ""),
        ("냉동실 용량", "Freezer capacity", "", "L", NUM, ""),
        ("연간 에너지 소비량", "Annual energy use", "Energy Consumption", "kWh/yr", NUM, ""),
        ("정격 전압", "Voltage", "", "V", NUM, ""),
        ("에너지스타 인증", "ENERGY STAR", "에너지스타", "", FLAG, ""),
    ]),
    ("냉각·신선", [
        ("듀얼 증발기", "Dual evaporator", "Twin Cooling", "", FLAG, ""),
        ("변환 서랍", "Convertible drawer", "FlexZone", "", FLAG, ""),
        ("공기청정 필터", "Air filter", "", "", FLAG, ""),
        ("급속 냉각", "Turbo cool", "Power Cool, Super Cool", "", FLAG, ""),
        ("휴가 모드", "Vacation mode", "", "", FLAG, ""),
        ("냉장실 설정 온도 범위", "Fridge setpoint range", "", "", TEXT, ""),
        ("냉동실 설정 온도 범위", "Freezer setpoint range", "", "", TEXT, ""),
    ]),
    ("편의 기능", [
        ("제빙기", "Ice maker", "Craft Ice", "", FLAG, ""),
        ("얼음 종류", "Ice type", "Cubed Ice, Crushed Ice", "", TEXT, ""),
        ("정수 필터", "Water filter", "", "", FLAG, ""),
        ("도어 개수", "Number of doors", "", "개", NUM, ""),
        ("소음", "Noise level", "", "dB", NUM, ""),
        ("정수 디스펜서", "Water dispenser", "", "", FLAG, ""),
        ("도어인도어", "Door-in-door", "InstaView", "", FLAG, ""),
        ("안식일 모드", "Sabbath mode", "", "", FLAG, ""),
        ("도어 알람", "Door alarm", "", "", FLAG, ""),
        ("도어 타입", "Door style", "", "", TEXT, ""),
        ("마감 색상", "Finish", "Color", "", TEXT, ""),
    ]),
    ("연결성", [
        ("Wi-Fi 연결", "Wi-Fi", "SmartThings, ThinQ", "", FLAG, ""),
        ("지문 방지 마감", "Fingerprint resistant", "", "", FLAG, ""),
    ]),
]

GROUPS = {"cooking": COOKING, "refrigerator": REFRIGERATOR}
THIN = Side(style="thin", color="BFBFBF")


def build(group: str = "cooking") -> bytes:
    """The sample form of a product group as .xlsx bytes."""
    if group not in GROUPS:
        raise ValueError(f"unknown sample group {group!r}")
    wb = Workbook()
    ws = wb.active
    ws.title = SHEETS[group]
    ws.append(HEADER)
    for c in range(1, len(HEADER) + 1):
        cell = ws.cell(1, c)
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="1F4E78")
        cell.alignment = Alignment(vertical="center", wrap_text=True, horizontal="center")
    ws.cell(1, 4).comment = Comment("쉼표(,) 또는 | 로 구분. 경쟁사가 쓰는 다른 표기를 적으면 그 표기가 우선 매칭됩니다.", "Gauge")
    ws.cell(1, 5).comment = Comment("mm, kg, L, W, kWh/yr, V, A, Hz 등. 단위가 다르면 자동 환산합니다. (in<->mm, cu ft<->L, lb<->kg ...)", "Gauge")
    ws.cell(1, 6).comment = Comment("숫자 / 예/아니오 / 텍스트 / 목록. 비워 두면 단위·이름을 보고 추론합니다.", "Gauge")
    row = 2
    for category, items in GROUPS[group]:
        first = row
        for ko, en, syn, unit, typ, note in items:
            ws.append([category, ko, en, syn, unit, typ, note])
            row += 1
        ws.merge_cells(start_row=first, start_column=1, end_row=row - 1, end_column=1)
        head = ws.cell(first, 1)
        head.font = Font(bold=True)
        head.alignment = Alignment(vertical="center", horizontal="center", wrap_text=True)
        head.fill = PatternFill("solid", fgColor="DDEBF7")
    for r in range(2, row):
        for c in range(1, len(HEADER) + 1):
            ws.cell(r, c).border = Border(top=THIN, bottom=THIN, left=THIN, right=THIN)
            if c > 1:
                ws.cell(r, c).alignment = Alignment(vertical="center", wrap_text=True)
    for i, w in enumerate([14, 24, 26, 36, 9, 11, 30], 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = "A2"
    dv = DataValidation(type="list", formula1=f'"{NUM},{FLAG},{TEXT},{LIST}"', allow_blank=True)
    ws.add_data_validation(dv)
    dv.add(f"F2:F{row - 1}")
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def main() -> None:
    out = ROOT / "data"
    out.mkdir(exist_ok=True)
    for group, name in FILES.items():
        data = build(group)
        (out / name).write_bytes(data)
        n = sum(len(items) for _, items in GROUPS[group])
        print(f"wrote data/{name}: {n} items, {len(GROUPS[group])} categories, {len(data)} bytes")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
