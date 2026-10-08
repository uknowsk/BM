"""Samsung Brazil adapter (https://www.samsung.com/br/): cooking appliances (contract: catalog.py).

Brazilian Portuguese site, prices in BRL. Samsung Brasil sells today: gas floor ranges ("fogao", oven included) = gas_oven,
a gas cooktop = gas_cooktop and built-in electric ovens = electric_oven. It sells NO microwave, speed-cook oven, OTR,
induction or radiant product (verified 2026-10-08: the cooking finder lists ranges, built-in ovens, a gas cooktop, hoods and
bundles only), so those sub keys are not in SUPPORTED_SUBCATEGORIES.

Data sources (public JSON / HTML the pages themselves load; plain requests work, no bot wall seen):
  discover: GET searchapi.samsung.com/v6/front/b2c/product/finder/global?type=08080000&siteCode=br&start=..&num=..
            (the product finder's own XHR): model code, Portuguese display name, prices, rating + review count,
            pdp url, keySummary. Hoods ("coifa") and kits/bundles ("kit-", model "F-...") are dropped by classify().
  scrape:   GET the PDP (spec table is server-rendered: .pdd32-product-spec__item sections with title / desc pairs,
            schema.org Product JSON-LD with name, sku, image, user-manual links on org.downloadcenter.samsung.com)
            + GET searchapi.../product/card/detail/global?modelList=<code> for the price block, rating and pvi type.
Price rule: the PDP shows "R$ 10.999,00 a vista (5% de desconto) ou R$ 11.577,89 em 18x sem juros". The regular price
(`afterTaxPrice`, the installment price without the cash discount) is stored as price_local; the a-vista price, the struck-
through "Preco original" and the 5% rule are recorded in extra_specs. No card/PIX/cash discount price is ever used, and no
currency conversion is done.
Rating / review count come from the finder (`ratings`, `reviewCount`) only when the site publishes them. Samsung BR exposes
no NEW badge and no release date, so is_new / release_date are never set.
Translation: labels/values are Portuguese; extra_specs are English via translate_many() (built-in cooking vocabulary first,
then i18n.get('pt')), RawSpec keeps the Portuguese original.
Robots: https://www.samsung.com/robots.txt allows /br/ for `User-agent: *` (the `Disallow: /br/` line belongs to the Yandex
group); only /*/search/, /*/c/p/ ... are blocked, none are used. Terms: public product data only, >= 1 s between requests.
Fetch order: requests, then headless Chromium, then a visible window (FRIDGE_BROWSER_MODE = auto | headless | visible);
cookie banner: decline first, accept only when it blocks the page. Hosts to allow: www.samsung.com (pages), images.samsung.com
(images) and org.downloadcenter.samsung.com (manual PDFs) are all covered by the existing "samsung.com" entry.
"""
import html as _html
import json
import os
import re
import sys
import time
import unicodedata
from urllib.parse import urljoin, urlsplit

import requests

import i18n
import units
from catalog import Candidate
from common import UA, download_pdf, launch_browser, looks_blocked
from schema import DocumentRecord, ProductRecord, RawSpec

BRAND = "Samsung"
COUNTRY = "br"
REGION = "sa"
CURRENCY = "BRL"
BASE = "https://www.samsung.com"
SITE = "/br/"
SEARCH_HOST = "searchapi.samsung.com"
FINDER_URL = f"https://{SEARCH_HOST}/v6/front/b2c/product/finder/global"
CARD_URL = f"https://{SEARCH_HOST}/v6/front/b2c/product/card/detail/global"
COOKING_TYPE = "08080000"
PAGE_SIZE = 50
MAX_PAGES = 6
MIN_DELAY_S = 1.0
MAX_REDIRECTS = 5
MAX_PDFS = 2
HEADERS = {"User-Agent": UA, "Accept-Language": "pt-BR,pt;q=0.9", "Accept": "application/json, text/html;q=0.9"}

# Sold today: see the module docstring. microwave / sco / otr / induction / radiant: not sold by Samsung Brasil.
SUPPORTED_SUBCATEGORIES = {"gas_oven", "gas_cooktop", "electric_oven"}
UNSUPPORTED_SUBCATEGORIES = {"microwave", "sco", "otr", "induction", "radiant"}
_SUB_FUEL = {"gas_oven": "gas", "gas_cooktop": "gas", "electric_oven": "electric", "radiant": "electric",
             "induction": "induction"}


class SamsungBrPageError(RuntimeError):
    """A Samsung BR page/API did not have the expected structure, left /br/, or was blocked."""


# ---------------------------------------------------------------- text / url helpers
def clean(text) -> str:
    """Unescaped, tag-free text with every kind of space (nbsp, Hangul filler U+3164 ...) collapsed."""
    s = re.sub(r"<[^>]*>", " ", _html.unescape(str(text or "")))
    s = s.replace("ㅤ", " ").replace("​", "")
    return " ".join(unicodedata.normalize("NFKC", s).split())


def fold(text: str) -> str:
    """Lower-case, accent-free text for matching ('Fogao' == 'fogao')."""
    s = unicodedata.normalize("NFKD", clean(text)).casefold()
    return "".join(c for c in s if not unicodedata.combining(c))


def _model(code: str) -> str:
    return (code or "").replace("/", "").strip()


def _safe_model(model: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", model).strip(".")


def _is_samsung_host(url: str) -> bool:
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
    except ValueError:
        return False
    return parts.scheme == "https" and not parts.username and (host == "samsung.com" or host.endswith(".samsung.com"))


def is_br_page(url: str) -> bool:
    """https samsung.com URL inside the Brazilian site (/br/...)."""
    return _is_samsung_host(url) and (urlsplit(url).path or "").startswith(SITE)


def _is_search_api(url: str) -> bool:
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    return (parts.scheme == "https" and (parts.hostname or "").lower() == SEARCH_HOST
            and parts.path.startswith("/v6/front/b2c/product/") and not parts.username)


def _allowed(url: str) -> bool:
    return is_br_page(url) or _is_search_api(url)


def _require_br(url: str) -> None:
    if not is_br_page(url):
        raise SamsungBrPageError(f"not a Samsung Brasil page (https samsung.com/br only): {url!r}")


def _abs(url) -> str | None:
    """Protocol-relative / relative -> https URL on a Samsung host, else None."""
    if not isinstance(url, str) or not url.strip():
        return None
    full = re.sub(r"^http://", "https://", urljoin(BASE + SITE, url.strip()))
    return full if _is_samsung_host(full) else None


# ---------------------------------------------------------------- number helpers
def pos_float(value) -> float | None:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


def pt_number(text) -> float | None:
    """First number of a Brazilian-formatted value ('12,7 kW', '3.300 W', '37,5 kg', '4.1 kW')."""
    return units.parse_eu_number(clean(text)) if re.search(r"\d", str(text or "")) else None


def fmt(x: float) -> str:
    return str(int(x)) if float(x) == int(x) else str(round(x, 2))


_NUMBER_PART = re.compile(r"(\d+(?:[.,]\d+)?)(?:\s+(\d+)/(\d+))?")


def _part_number(part: str) -> float | None:
    """First number of one 'W x H x D' part; '29 15/16' is a mixed fraction, '36 ~ 36 3/4' keeps its first number."""
    m = _NUMBER_PART.search(part)
    if not m:
        return None
    whole = float(m.group(1).replace(",", "."))
    return whole + int(m.group(2)) / int(m.group(3)) if m.group(2) else whole


def dims_in(value: str, nd: int = 1) -> tuple[float | None, float | None, float | None]:
    """'W x H x D' text -> inches. mm by default, cm / polegada(s) / in by unit; a range '(914.4 ~ 933.5)' keeps its first
    number; mixed fractions '(29 15/16)' are read. (None, None, None) unless three numbers are present. `nd` = decimals of the result."""
    text = clean(value)
    parts = re.split(r"\s*[x×]\s*", text)
    nums = [_part_number(p) for p in parts[:3]]
    if len(parts) < 3 or any(n is None for n in nums):
        return None, None, None
    low = text.lower()
    if re.search(r"polegada|\bin\b|\"", low):
        factor = 1.0
    elif re.search(r"\bcm\b", low) and "mm" not in low:
        factor = 1 / 2.54
    else:
        factor = 1 / units.MM_PER_IN
    w, h, d = (round(n * factor, nd) for n in nums)
    return w, h, d


def yes_no(value: str | None) -> bool | None:
    v = fold(value or "")
    if not v:
        return None
    if re.match(r"(sim\b|yes\b|\d)", v):
        return True
    if re.match(r"(nao\b|no\b|-$)", v):
        return False
    return True


# ---------------------------------------------------------------- classification (shared by discover and scrape)
def classify(name: str, model: str = "", slug: str = "", pvi: str = "") -> str | None:
    """THE single classifier: catalog sub key or None (hoods, kits/bundles, toaster-type ovens, unknown). `name` is the
    Portuguese display name, `pvi` Samsung's English pviSubtypeName ('Gas Range', 'Electric Oven', 'Cooktop') when known.
    Rules are ordered and exclusive: coifa/kit first, then micro-ondas, cooktop, fogao, forno."""
    n = fold(name)
    p = fold(pvi)
    if (slug or "").lower().startswith("kit-") or (model or "").upper().startswith("F-"):
        return None  # bundle: cooktop + hood + oven, or range + fridge
    if (re.search(r"coifa|depurador|exaustor|\bhood\b", n) and not re.search(r"micro-?ondas", n)) or p == "hood":
        return None
    gas = bool(re.search(r"\bga[sz]\b|glp|queimador|boca", n))
    if re.search(r"micro-?ondas", n):
        if re.search(r"coifa|sobre o fogao|over[- ]the[- ]range", n):
            return "otr"
        if re.search(r"forno (eletrico|a gas|de convec)|convec|combina|combo", n):
            return "sco"
        return "microwave"
    if re.search(r"cooktop|placa", n) or p == "cooktop":
        if re.search(r"indu", n):
            return "induction"
        if re.search(r"vitroceram|eletric|ceramic|radiant", n):
            return "radiant"
        return "gas_cooktop" if gas or p == "cooktop" else None
    if re.search(r"fogao|\brange\b", n) or p in ("gas range", "electric range", "range"):
        if re.search(r"fogao de embutir|embutir", n) and "forno" not in n.replace("fogao", ""):
            return "gas_cooktop" if gas else None
        if re.search(r"indu", n):
            return "induction"
        if re.search(r"vitroceram|eletric|ceramic|radiant", n) or p == "electric range":
            return "radiant"
        return "gas_oven"
    if re.search(r"forno", n) or p in ("electric oven", "gas oven", "oven"):
        if re.search(r"bancada|toaster|mini ?forno|fritadeira|air ?fryer oven", n):
            return None  # tabletop / toaster-type oven
        return "gas_oven" if (re.search(r"\bga[sz]\b|glp", n) or p == "gas oven") else "electric_oven"
    return None


# ---------------------------------------------------------------- translation (Portuguese -> English)
# Curated vocabulary of the Samsung BR / LG BR cooking spec tables (keys are matched accent-, case- and space-
# insensitively); anything missing goes to i18n.get('pt') (glossary, cache, local LLM) and otherwise stays Portuguese.
PT_LABELS = {
    # sections
    "Tipo": "Type", "Capacidade": "Capacity", "Materiais/Acabamentos": "Materials/Finishes", "Recursos": "Features",
    "Cooktop": "Cooktop", "Característica geral": "General features", "Desempenho": "Performance",
    "Drawer": "Drawer", "Pesos/dimensões": "Weights/dimensions", "Acessórios": "Accessories", "Smart": "Smart",
    "Conectividade com aplicativo": "App connectivity",
    # type / capacity
    "Tipo de instalação": "Installation type", "Tipo de cavidade": "Cavity type", "Capacidade do forno": "Oven capacity",
    "Capacidade da gaveta": "Drawer capacity", "Tipo de Forno": "Oven type", "Material da cavidade": "Cavity material",
    "Installation Type": "Installation type", "Control Type": "Control type", "Cor": "Color",
    # finishes / controls
    "Cor do Forno": "Oven color", "Tipo de controle (forno)": "Control type (oven)",
    "Tipo de controle (cooktop)": "Control type (cooktop)", "Tipo de display": "Display type",
    "Cor do visor": "Display color", "Tipo de Porta": "Door type", "Porta de fechamento suave": "Soft-close door",
    "Estrutura do cooktop": "Cooktop structure",
    # features
    "Air Fryer": "Air fry", "Air Sous Vide": "Air sous vide", "Auto-Limpeza": "Self-clean", "Limpeza Vapor": "Steam clean",
    "Método de limpeza": "Cleaning method", "Conexão Wi-Fi": "Wi-Fi connection", "Culinária fácil": "Easy cooking",
    "Culinária saudável": "Healthy cooking", "Culinária favorita": "Favorite cooking", "Manter quente": "Keep warm",
    "Relógio": "Clock", "Teclado numérico": "Numeric keypad", "Cronômetro de cozinha": "Kitchen timer",
    "Timer": "Timer", "Trava para crianças": "Child lock", "Hidden Bake Element": "Hidden bake element",
    "Luz Interior": "Interior light", "Lâmpada": "Lamp", "Luz (lâmpada) Ligar/Desligar": "Light (lamp) on/off",
    "Som ligado/desligado": "Sound on/off",
    "Configuração (opção do sistema de relógio (12H/24H))": "Clock system setting (12H/24H)",
    "Adiar início": "Delay start", "Modo Sabbath": "Sabbath mode", "Comandos de voz": "Voice commands",
    "Programas Automáticos": "Automatic programs", "Camera": "Camera", "Opções de Idiomas": "Language options",
    "Tipo de Vapor": "Steam type", "Grill Superior": "Top grill", "Grill Inferior": "Bottom grill",
    "Modo Único (Grill Superior + Convecção)": "Single mode (top grill + convection)",
    "Modo Único (Grill)": "Single mode (grill)", "Modo Único (Eco Grill)": "Single mode (eco grill)",
    "Modo Único (Grill inferior + Convecção)": "Single mode (bottom grill + convection)",
    "Convection (W)": "Convection (W)", "Convecção": "Convection",
    # cooktop / fuel
    "Tipo de Gás": "Gas type", "Tipo de combustível": "Fuel type", "Número de queimadores": "Number of burners",
    "Potência Total": "Total power", "Potência total (kW)": "Total power (kW)", "Nível de potência": "Power levels",
    "Centro oval": "Oval center burner", "Queimadores Selados": "Sealed burners", "Grade": "Grate/rack",
    "Tampa do queimador": "Burner cap", "Fonte de alimentação": "Power supply", "Voltagem": "Voltage",
    "Potência Saída": "Output power",
    # oven performance
    "Assar (único)": "Bake (single)", "Grelha com ajuste variável": "Variable broil",
    "Grelha com ajuste variável (baixa-alta) (superior)": "Variable broil (low-high) (upper)",
    "Assar por convecção (parte superior)": "Convection bake (upper)",
    "Assado por convecção (parte superior)": "Convection roast (upper)",
    "Convection Bake (Single)": "Convection bake (single)", "Convection Roast (Single)": "Convection roast (single)",
    "Potência (Assar)": "Power (bake)", "Potência (Grelhar)": "Power (broil)",
    "Potência (aquecedor de convecção)": "Power (convection heater)", "Temperatura Forno": "Oven temperature",
    "Temperatura Forno (Superior/Inferior)": "Oven temperature (upper/lower)",
    "Temperatura Forno (Combinado)": "Oven temperature (combined)", "Trilho": "Rail",
    # dimensions / weight
    "Dimensões do Produto s/ embalagem (LxAxP)": "Product dimensions without packaging (WxHxD)",
    "Dimensões do Produto c/ embalagem (LxAxP)": "Product dimensions with packaging (WxHxD)",
    "Dimensões Produto": "Product dimensions", "Dimensões Embalagem": "Package dimensions",
    "Package Dimension (WxHxD)(mm)": "Package dimensions (WxHxD) (mm)", "Corte": "Cut-out",
    "Corte (L x A x P)": "Cut-out (WxHxD)", "Peso (líquido)": "Net weight", "Peso (bruto)": "Gross weight",
    "Líquido (L x A x P, polegada)": "Net (WxHxD, inches)", "Bruto (L x A x P, polegada)": "Gross (WxHxD, inches)",
    "Quantidade de carregamento (20/40 pés)": "Loading quantity (20/40 ft)",
    # accessories
    "Número de posições do rack": "Number of rack positions", "Cesta Air Fryer": "Air fry basket",
    "Grade de grelhados": "Grilling rack", "Grelha de arame": "Wire rack", "Chapa": "Griddle",
    "Sonda de Temperatura": "Temperature probe", "Termômetro": "Thermometer", "Grade para WOK": "Wok grate",
    "Dispositivo antitombamento": "Anti-tip device", "Kit de conversão para Gás LP": "LP gas conversion kit",
    "Bandeja de panificação": "Baking tray", "Grade deslizante": "Sliding rack", "Forma": "Baking tray",
    "Trilho Telescópico": "Telescopic rail", "Recipiente (Vapor)": "Steam container",
    # smart
    "Wi-Fi embutido": "Built-in Wi-Fi", "Suporte para o aplicativo SmartThings": "SmartThings app support",
    # --- LG BR microwave / oven tables
    "ESPECIFICAÇÕES BÁSICAS": "Basic specifications", "POTÊNCIA / CLASSIFICAÇÕES": "Power / ratings",
    "RECURSOS CONVENIENTES": "Convenience features", "DESIGN / ACABAMENTO": "Design / finish",
    "CARACTERÍSTICAS DO FORNO MICRO-ONDAS": "Microwave oven characteristics", "DIMENSÕES / PESO": "Dimensions / weight",
    "TECNOLOGIA INTELIGENTE": "Smart technology", "RECURSOS DE CONTROLE": "Control features",
    "MODOS DE COZIMENTO": "Cooking modes", "ACESSÓRIOS": "Accessories", "CÓDIGO DE BARRAS": "Barcode",
    "ENERGIA": "Energy", "CARACTERÍSTICAS DO FORNO": "Oven characteristics",
    "RECURSOS DO RECIPIENTE": "Drawer features",
    "Marca": "Brand", "País de origem": "Country of origin", "Cor da porta": "Door color",
    "Design da porta": "Door design", "Limpa Fácil": "EasyClean", "Cor externa": "Exterior color",
    "Capacidade do forno (L)": "Oven capacity (L)", "Saída de alimentação (W)": "Power output (W)",
    "Fonte de alimentação necessária (Volt/Hz)": "Required power supply (V/Hz)",
    "Adicionar 30 segundos": "Add 30 seconds", "Bloqueio infantil": "Child lock", "Bipe de conclusão": "End-of-cooking beep",
    "Ajuste de hora": "Time setting", "Ligar/desligar prato giratório": "Turntable on/off",
    "Indicador de Porta Fechada": "Door-closed indicator", "Design da cavidade": "Cavity design",
    "Design da porta de vidro": "Glass door design", "Design exterior": "Exterior design", "Cor interna": "Interior color",
    "Acabamento PrintProof": "PrintProof finish", "Tipo de luz da cavidade": "Cavity light type",
    "Como cozinhar": "How to cook", "Consumo de energia do micro-ondas (W)": "Microwave power consumption (W)",
    "Níveis de potência do micro-ondas": "Microwave power levels", "Potência útil do micro-ondas (W)": "Microwave output power (W)",
    "Smart Inverter": "Smart Inverter", "Consumo de energia total (W)": "Total power consumption (W)",
    "Tamanho do prato giratório (mm)": "Turntable size (mm)", "Peso para expedição (kg)": "Shipping weight (kg)",
    "Dimensão da cavidade (L x A x P) (mm)": "Cavity dimensions (WxHxD) (mm)",
    "Dimensões da embalagem (L x A x P) (mm)": "Package dimensions (WxHxD) (mm)",
    "Dimensões do produto (L x A x P) (mm)": "Product dimensions (WxHxD) (mm)", "Peso do produto (kg)": "Product weight (kg)",
    "NFC Tag On": "NFC Tag On", "SmartDiagnosis": "SmartDiagnosis", "ThinQ (Wi-Fi)": "ThinQ (Wi-Fi)",
    "Visor de controle": "Control display", "Local do controle": "Control location", "Tipo de controle": "Control type",
    "Fritura a ar": "Air fry", "Auto cozimento": "Auto cook", "Auto reaquecimento": "Auto reheat", "Cozer": "Bake",
    "Cozer por Convecção": "Convection bake", "Descongelar": "Defrost", "Desidratar": "Dehydrate", "Grill": "Grill",
    "Descongelamento Inverter": "Inverter defrost", "Derreter": "Melt", "Cozimento de Memória": "Memory cooking",
    "Prova": "Proof", "Assar": "Roast", "Sensor Cook": "Sensor cook", "Sensor Reheat": "Sensor reheat",
    "Cozimento Lento": "Slow cook", "Amolecer": "Soften", "Convecção Rápida": "Quick convection",
    "Grill Rápido": "Quick grill", "Cocção em Estágios": "Staged cooking", "Cozinheiro a Vapor": "Steam cooker",
    "Aquecer": "Warm", "Bandeja de vidro (un.)": "Glass tray (pcs)", "Anel giratório (un.)": "Roller ring (pcs)",
    "Manual do usuário (un.)": "User manual (pcs)", "Código de Barras": "Barcode",
    "Classe de Eficiência Energética": "Energy efficiency class", "Tipo de forno": "Oven type",
    "Sistema de cozimento do forno": "Oven cooking system", "Tipo de abastecimento": "Supply type",
    "Bloqueio de controle": "Control lock", "Tipo de limpeza do forno": "Oven cleaning type",
    "Sistema de fechamento suave": "Soft-close system", "Cozimento cronometrado": "Timed cooking",
    "Consumo de energia da convecção (W)": "Convection power consumption (W)",
    "Consumo de energia do grill (W)": "Grill power consumption (W)",
    "Consumo de energia combinado (MO+Conv.) (W)": "Combined power consumption (MW+Conv.) (W)",
    "Consumo de energia combinado (MO+grill) (W)": "Combined power consumption (MW+grill) (W)",
    "Potência do elemento de cozer (W)": "Bake element power (W)", "Potência do elemento de tostar (W)": "Broil element power (W)",
    "Potência do elemento de convecção (W)": "Convection element power (W)", "Ventilador de convecção": "Convection fan",
    "Tipo de convecção": "Convection type", "Número de prateleiras do forno": "Number of oven racks",
    "Número de posições de prateleira": "Number of rack positions", "Modo de cozimento do forno": "Oven cooking mode",
    "Tipo de luz do forno": "Oven light type", "Tipo de gaveta": "Drawer type",
    "Controlar CFM por Wi-Fi": "Control CFM via Wi-Fi", "Controlar Luzes por Wi-Fi": "Control lights via Wi-Fi",
    "Ligar/Desligar Energia por Wi-Fi": "Power on/off via Wi-Fi", "Dimensões de corte (L x A x P) (mm)": "Cut-out dimensions (WxHxD) (mm)",
    "Tamanho do disjuntor (ampere)": "Breaker size (amp)",
}
PT_VALUES = {
    "Sim": "Yes", "Não": "No", "Nao": "No", "Inox": "Stainless steel", "Preto": "Black", "Branco": "White",
    "Black": "Black", "Stainless": "Stainless steel", "Cinza": "Gray", "Prata": "Silver", "Fumê": "Smoked",
    "Prata nobre": "Noble silver", "Espelhado": "Mirror", "Brazil": "Brazil", "Brasil": "Brazil", "China": "China",
    "Deslize para dentro": "Slide-in", "Slide-in": "Slide-in", "Built-in": "Built-in", "Single": "Single",
    "Porta única": "Single door", "Porta única (4 STSS Layers)": "Single door (4 STSS layers)", "Traseira": "Rear", "Armazenar": "Storage", "Touch": "Touch", "Botão": "Knob",
    "Manípulo": "Knob", "Gás": "Gas", "GLP": "LPG", "Gás LP": "LPG", "Gás Natural": "Natural gas",
    "Gás LP – Botijão": "LPG (cylinder)", "Gás LP - Botijão": "LPG (cylinder)", "Catalítico": "Catalytic",
    "Cerâmica esmaltada": "Enamel ceramic", "Abertura horizontal": "Horizontal opening", "Dual Cook": "Dual Cook",
    "Esmalte Porcelana Preto": "Black porcelain enamel", "Cast Iron (3pcs)": "Cast iron (3 pcs)",
    "Aluminum Griddle": "Aluminum griddle", "Superior/lateral": "Upper/side", "Alto / Baixo": "High / Low",
    "Dividida": "Split", "Em bancada": "Countertop", "Quadrada": "Square", "Visão ampla tradicional": "Traditional wide view",
    "Incandescente": "Incandescent", "Automático + Manual": "Automatic + Manual", "Lado direito": "Right side",
    "Teclado de membrana": "Membrane keypad", "Painel Touch": "Touch panel", "Toque em vidro": "Glass touch",
    "Inteira": "Full", "Vertical direita": "Vertical right", "LED": "LED", "Elétrico": "Electric",
}
# word-level fallback for short positional texts ('Direita / Frente - 4,1 kW', 'Queimador - Dianteiro esquerdo')
_PT_WORDS = {
    "direita": "Right", "esquerda": "Left", "frente": "Front", "traseira": "Rear", "centro": "Center", "central": "Center",
    "dianteiro": "Front", "traseiro": "Rear", "esquerdo": "Left", "direito": "Right", "queimador": "Burner",
    "queimadores": "burners", "baixo": "Low", "alto": "High", "medio": "Medium", "de": "of", "do": "of", "da": "of",
}


def _build_index(table: dict[str, str]) -> dict[str, str]:
    return {i18n.fold(k): v for k, v in table.items()}


_LABEL_INDEX = _build_index(PT_LABELS)
_VALUE_INDEX = _build_index(PT_VALUES)


def _builtin(text: str, kind: str) -> str | None:
    """English for `text` from the curated tables / word rules, or None when only i18n (or nothing) can help."""
    t = clean(text)
    if not t:
        return ""
    key = i18n.fold(t)
    hit = (_LABEL_INDEX if kind == "label" else _VALUE_INDEX).get(key)
    if hit is None and kind == "label":
        m = re.fullmatch(r"Queimador (\d+)", t, re.I)
        if m:
            return f"Burner {m.group(1)}"
    if hit is None and kind == "value":
        m = re.fullmatch(r"(Sim|Não)\s*\((.+)\)", t, re.I)  # 'Sim (1)' -> 'Yes (1)'
        if m:
            return f"{'Yes' if m.group(1).lower() == 'sim' else 'No'} ({m.group(2)})"
    if hit is None and kind == "value" and key in _LABEL_INDEX and not re.search(r"\d", t):
        hit = _LABEL_INDEX[key]
    if hit is not None:
        return hit
    pieces = re.split(r"(\s*[/\-–()+,]\s*|\s+)", t)
    out, translated = [], False
    for piece in pieces:
        w = fold(piece)
        if not w or re.fullmatch(r"[\s/\-–()+,]+", piece):
            out.append(piece)
        elif w in _PT_WORDS:
            out.append(_PT_WORDS[w])
            translated = True
        elif re.fullmatch(r"[\d.,]+|[a-z]{1,3}\.?|x", w):  # numbers and units (kW, mm, V ...)
            out.append(piece)
        else:
            return None
    return "".join(out) if translated else None


def _pt():
    """The shared i18n Translator for Brazilian Portuguese, or None when unavailable (tests patch this)."""
    try:
        return i18n.get("pt")
    except Exception:  # noqa: BLE001 - translation is best effort; the Portuguese original is kept in RawSpec
        return None


def translate_many(texts: list[str], kind: str = "value") -> list[str]:
    """English for each text (kind 'label' | 'value'): curated vocabulary first, then i18n 'pt'; unknown text is returned
    unchanged (never invented)."""
    out = [_builtin(t, kind) for t in texts]
    todo = [i for i, r in enumerate(out) if r is None]
    if todo:
        tr = _pt()
        got = None
        if tr is not None:
            try:
                got = tr.translate_many([texts[i] for i in todo], kind)
            except Exception as exc:  # noqa: BLE001
                print(f"samsung_br: i18n pt unavailable ({type(exc).__name__})", file=sys.stderr)
        for j, i in enumerate(todo):
            out[i] = got[j] if got else clean(texts[i])
    return out


# ---------------------------------------------------------------- HTTP (requests first, browser fallback)
_CONSENT_DECLINE = ("Rejeitar", "Recusar", "Somente necess", "Apenas necess", "Reject", "Decline", "Necessary")
_CONSENT_ACCEPT = ("Aceitar", "Concordo", "Permitir", "Accept", "Allow")

_FETCH_JS = """async ({u, m, h, b}) => {
  const same = new URL(u, location.href).origin === location.origin;  // cross-origin APIs (searchapi, coveo): no cookies
  const o = {method: m, redirect: 'manual', credentials: same ? 'include' : 'omit', headers: h || {}};
  if (b !== null && b !== undefined) { o.body = JSON.stringify(b); o.headers['Content-Type'] = 'application/json'; }
  const r = await fetch(u, o);
  if (r.type === 'opaqueredirect') return {s: 'redirect', t: ''};
  return {s: r.status, t: await r.text()};
}"""


class Net:
    """HTTP for one discover()/scrape() run. Plain requests first (except FRIDGE_BROWSER_MODE=visible); when a response is
    not usable (status or `ok(text)` check fails: bot wall, 403/429 ...) the same request is replayed inside a real browser
    page on the site's origin. Mode (read on every call): auto = requests, headless, visible; headless = requests,
    headless; visible = visible only. Every hop and the final URL must satisfy `allowed`. The browser is closed in close().
    Requests are >= MIN_DELAY_S apart per host. Shared with lg_br (a different `allowed` / `open_url`)."""

    _last: dict[str, float] = {}

    def __init__(self, open_url: str, allowed, name: str = "samsung_br", lang: str = "pt-BR", error=SamsungBrPageError):
        self.open_url, self.allowed, self.name, self.lang, self.error = open_url, allowed, name, lang, error
        self._pw = self._browser = self._page = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self) -> None:
        for obj, method in ((self._browser, "close"), (self._pw, "stop")):
            try:
                if obj is not None:
                    getattr(obj, method)()
            except Exception:  # noqa: BLE001 - shutting down; nothing useful to do with a teardown failure
                pass
        self._pw = self._browser = self._page = None

    def _throttle(self, url: str) -> None:
        host = (urlsplit(url).hostname or "").lower()
        wait = MIN_DELAY_S - (time.monotonic() - Net._last.get(host, 0.0))
        if wait > 0:
            time.sleep(wait)
        Net._last[host] = time.monotonic()

    def _check(self, url: str) -> None:
        if not self.allowed(url):
            raise self.error(f"{self.name}: URL outside the allowed site (geo redirect or foreign url): {url!r}")

    def _plain(self, method: str, url: str, headers: dict | None, body) -> tuple[int, str]:
        cur, meth = url, method
        for _hop in range(MAX_REDIRECTS + 1):
            self._check(cur)
            self._throttle(cur)
            r = requests.request(meth, cur, json=body if meth == "POST" else None,
                                 headers={**HEADERS, **(headers or {})}, timeout=30, allow_redirects=False)
            if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("Location"):
                cur, meth, body = urljoin(cur, r.headers["Location"]), "GET", None
                continue
            r.encoding = "utf-8"
            return r.status_code, r.text
        raise self.error(f"{self.name}: more than {MAX_REDIRECTS} redirects for {url}")

    def _consent(self, accept: bool) -> bool:
        for w in (_CONSENT_ACCEPT if accept else _CONSENT_DECLINE):
            try:
                loc = self._page.locator(f"button:visible:has-text('{w}')").first
                if loc.count():
                    loc.click(timeout=2000)
                    return True
            except Exception:  # noqa: BLE001 - playwright error types vary; banner handling is best effort
                continue
        return False

    def _open(self, headless: bool) -> None:
        from playwright.sync_api import sync_playwright
        self.close()
        self._pw = sync_playwright().start()
        self._browser = launch_browser(self._pw, headless=headless)
        page = self._browser.new_context(user_agent=UA, locale=self.lang).new_page()
        self._throttle(self.open_url)
        page.goto(self.open_url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(3000)
        self._check(page.url)
        self._page = page
        if not self._consent(False) and looks_blocked(None, page.inner_text("body")):
            self._consent(True)  # decline first; accept only when the banner actually blocks the page

    def _in_browser(self, method: str, url: str, headers, body, as_page: bool) -> tuple[int | str, str]:
        self._throttle(url)
        if as_page:
            resp = self._page.goto(url, wait_until="domcontentloaded", timeout=60000)
            self._page.wait_for_timeout(2500)
            self._check(self._page.url)
            return (resp.status if resp else 0), self._page.content()
        res = self._page.evaluate(_FETCH_JS, {"u": url, "m": method, "h": headers or {}, "b": body})
        if res.get("s") == "redirect":
            raise self.error(f"{self.name}: unexpected redirect for {url} (geo?)")
        return res["s"], res["t"]

    def text(self, url: str, *, method: str = "GET", headers: dict | None = None, body=None, ok=None,
             as_page: bool = False) -> str:
        """Response text. `ok(text)` is the shape check (None accepts any 2xx). `as_page` loads the URL as a document in
        the browser fallback (needed for server-rendered pages), otherwise a fetch() on the open origin is used."""
        self._check(url)
        mode = os.environ.get("FRIDGE_BROWSER_MODE", "auto").strip().lower()
        good = lambda st, t: isinstance(st, int) and 200 <= st < 300 and (ok is None or ok(t))
        if mode != "visible":
            try:
                st, text = self._plain(method, url, headers, body)
            except requests.RequestException:
                st, text = None, ""
            if good(st, text):
                return text
        for headless in {"headless": [True], "visible": [False]}.get(mode, [True, False]):
            try:
                if self._page is None:
                    self._open(headless)
                st, text = self._in_browser(method, url, headers, body, as_page)
            except self.error:
                raise
            except Exception as e:  # noqa: BLE001 - playwright raises its own error types
                print(f"{self.name}: playwright headless={headless} failed: {e}", file=sys.stderr)
                self.close()
                continue
            if good(st, text):
                return text
            print(f"{self.name}: browser headless={headless} got status {st}", file=sys.stderr)
            self.close()
        raise self.error(f"{self.name}: could not load {url} (blocked or unexpected response; mode={mode})")


def _net() -> Net:
    return Net(BASE + SITE + "cooking-appliances/", _allowed)


# ---------------------------------------------------------------- price / signals
def _price_from_model(m: dict) -> dict:
    """Prices of one finder / card model dict. `regular` = the price WITHOUT the a-vista (cash) discount: `afterTaxPrice`
    (what the PDP shows as 'ou R$ X em 18x sem juros'); `cash` = the headline `price` (a vista, 5% off); `original` = the
    struck-through 'Preco original' (rrpPriceDisplay). Only `regular` is stored as the product price."""
    cash = pos_float(m.get("price"))
    regular = pos_float(m.get("afterTaxPrice"))
    if regular is None or (cash is not None and regular < cash):
        regular = cash
    original = pt_number(m.get("rrpPriceDisplay")) if m.get("rrpPriceDisplay") else None
    return {"regular": regular, "cash": cash, "original": pos_float(original)}


def _signals(m: dict) -> dict:
    """rating (0-5) / review_count when the site publishes them; nothing else (no NEW badge or release date on this site)."""
    out: dict = {}
    rating, count = pos_float(m.get("ratings")), None
    try:
        count = int(m.get("reviewCount"))
    except (TypeError, ValueError):
        pass
    if rating is not None and rating <= 5 and count and count > 0:
        out["rating"] = round(rating, 2)
        out["review_count"] = count
    return out


# ---------------------------------------------------------------- discover
def _listing_attrs(sub: str, name: str, m: dict) -> dict:
    n = fold(name)
    attrs: dict = {"fuel": _SUB_FUEL[sub]} if sub in _SUB_FUEL else {}
    liters = re.search(r"(?<![\d.,])(\d{2,3})\s*l\b", n)
    if liters and sub == "electric_oven":
        cuft = round(units.l_to_cuft(float(liters.group(1))), 2)
        attrs["capacity_total_cuft"] = cuft
        attrs["oven_capacity_cuft"] = cuft
    burners = re.search(r"(\d+)\s*(?:bocas|queimadores)", n)
    if burners:
        attrs["burners"] = int(burners.group(1))
    for item in m.get("keySummary") or []:
        key, value = fold(item.get("key") or ""), item.get("value") or ""
        if key.startswith("dimensoes produto") and "width_in" not in attrs:
            w = dims_in(value)[0]
            if w:
                attrs["width_in"] = w
        elif key == "numero de queimadores" and "burners" not in attrs and (b := re.search(r"\d+", value)):
            attrs["burners"] = int(b.group(0))
    if re.search(r"wi-?fi", n):
        attrs["wifi"] = True
    if re.search(r"air ?fry", n):
        attrs["air_fry"] = True
    attrs.update(_signals(m))
    return attrs


def _candidate(m: dict, sub: str) -> Candidate | None:
    path = m.get("pdpUrl") or m.get("originPdpUrl") or ""
    url = _abs(path)
    code = _model(m.get("modelCode") or "")
    if not url or not is_br_page(url) or not code:
        return None
    name = clean(m.get("displayName") or code)
    attrs = _listing_attrs(sub, name, m)
    return Candidate(brand=BRAND, model_number=code, name=name, url=url, price_usd=None, category="cooking",
                     subcategory=sub, region=REGION, country=COUNTRY, currency=CURRENCY,
                     price_local=_price_from_model(m)["regular"], attrs=attrs,
                     attrs_src={k: "listing" if k in ("rating", "review_count", "width_in", "burners") else "name"
                                for k in attrs})


def parse_finder(payload: dict, sub: str | None = None, limit: int = 10 ** 6) -> list[Candidate]:
    """Finder JSON -> Candidates (one per model, deduped by model code). With `sub`, only products classify() files there."""
    try:
        families = payload["response"]["resultData"]["productList"]
    except (KeyError, TypeError) as e:
        raise SamsungBrPageError(f"unexpected finder payload ({e!r})") from e
    out: dict[str, Candidate] = {}
    for fam in families:
        for m in fam.get("modelList") or []:
            name = m.get("displayName") or fam.get("fmyMarketingName") or ""
            slug = (urlsplit(m.get("pdpUrl") or "").path.rstrip("/").rsplit("/", 1) + [""])[-1]
            found = classify(name, m.get("modelCode") or "", slug, m.get("pviSubtypeName") or "")
            if found is None or (sub is not None and found != sub):
                continue
            c = _candidate(m, found)
            if c and c.model_number not in out:
                out[c.model_number] = c
                if len(out) >= limit:
                    return list(out.values())
    return list(out.values())


def _finder_url(start: int) -> str:
    return (f"{FINDER_URL}?type={COOKING_TYPE}&siteCode=br&start={start}&num={PAGE_SIZE}&sort=recommended"
            "&onlyFilterInfoYN=N&keySummaryYN=Y")


def discover(subcategory: str, limit: int = 30) -> list[Candidate]:
    if subcategory not in SUPPORTED_SUBCATEGORIES:
        raise ValueError(f"Samsung BR adapter does not support sub category {subcategory!r}")
    found: dict[str, Candidate] = {}
    with _net() as net:
        start, total = 1, None
        for _page in range(MAX_PAGES):
            text = net.text(_finder_url(start), ok=lambda t: t.lstrip().startswith("{"))
            try:
                payload = json.loads(text)
                common = payload["response"]["resultData"]["common"]
                total, to = int(common["totalRecord"]), int(common["toRecord"])
            except (ValueError, KeyError, TypeError) as e:
                raise SamsungBrPageError(f"unexpected finder payload ({e!r})") from e
            for c in parse_finder(payload, subcategory):
                found.setdefault(c.model_number, c)
            if len(found) >= limit or to >= total:
                break
            start = to + 1
    return list(found.values())[:limit]


# ---------------------------------------------------------------- PDP parsing
_SPEC_ITEM = re.compile(r'<div class="pdd32-product-spec__item">(.*?)(?=<div class="pdd32-product-spec__item">|\Z)', re.S)
_SPEC_PAIR = re.compile(r'content-item-title">(.*?)</p>\s*<p class="pdd32-product-spec__content-item-desc">(.*?)</p>', re.S)
_LD_JSON = re.compile(r'<script type="application/ld\+json"[^>]*>(.*?)</script>', re.S)


def parse_spec_rows(page_html: str) -> list[tuple[str, str, str]]:
    """Every row of the server-rendered spec table: (section, label, value) in Portuguese, page order."""
    rows = []
    for block in _SPEC_ITEM.findall(page_html):
        title = re.search(r'accordion:([^"]+)"', block)
        section = clean(title.group(1)) if title else ""
        for label, value in _SPEC_PAIR.findall(block):
            label, value = clean(label), clean(value)
            if label and value:
                rows.append((section, label, value))
    return rows


def parse_product_ld(page_html: str) -> dict:
    """The schema.org Product block of a PDP ({} when absent)."""
    for raw in _LD_JSON.findall(page_html):
        try:
            data = json.loads(raw)
        except ValueError:
            continue
        for item in data if isinstance(data, list) else [data]:
            kind = item.get("@type") if isinstance(item, dict) else None
            kinds = kind if isinstance(kind, list) else [kind]
            if any(isinstance(k, str) and k.lower() == "product" for k in kinds):
                return item
    return {}


def parse_manuals(page_html: str) -> list[dict]:
    """User-manual PDF links (org.downloadcenter.samsung.com) as {url, lang}, Portuguese first, deduped."""
    found = []
    for tag in re.findall(r"<a [^>]*ContentsFile\.aspx[^>]*>", page_html):
        href = re.search(r'href="([^"]+)"', tag)
        lang = re.search(r'data-accept-lang="([^"]*)"', tag)
        cat = re.search(r'data-category="([^"]*)"', tag)
        url = _abs(_html.unescape(href.group(1))) if href else None
        if url and (not cat or cat.group(1) == "manual") and not any(f["url"] == url for f in found):
            found.append({"url": url, "lang": fold(lang.group(1)) if lang else ""})
    found.sort(key=lambda f: 0 if f["lang"].startswith("portugu") else 1)
    return found


# ---------------------------------------------------------------- record building
def _pick(rows, label_pat: str, section_pat: str | None = None) -> str | None:
    """Value of the first row whose folded label fully matches label_pat (and section matches, if given)."""
    for sec, label, value in rows:
        if re.fullmatch(label_pat, fold(label)) and (section_pat is None or re.search(section_pat, fold(sec))):
            return value
    return None


def _dimensions(rows, nd: int = 1) -> tuple[float | None, float | None, float | None]:
    """Exterior (w, h, d) in inches: the product-dimension row without packaging, else the net inch row."""
    best = None
    for sec, label, value in rows:
        f = fold(label)
        if re.search(r"dimens(oes|ao).*produto", f) and not re.search(r"c/ ?embalagem|com embalagem", f):
            best = dims_in(value, nd)
            if best[0]:
                return best
        if f.startswith("liquido") and "polegada" in fold(value) and (best is None or not best[0]):
            best = dims_in(value, nd)
    return best or (None, None, None)


def _electrical(rows) -> tuple[str | None, float | None]:
    value = _pick(rows, r"voltagem|fonte de alimentacao")
    if not value:
        return None, None
    v = re.search(r"(\d{2,3})\s*V", value)
    hz = re.search(r"(\d{2})\s*Hz", value, re.I)
    return (v.group(1) if v else None), (float(hz.group(1)) if hz else None)


def _wifi(rows) -> tuple[bool | None, str | None]:
    """(supported, evidence = 'Section > Label = Value' in the original Portuguese)."""
    answers = []
    for sec, label, value in rows:
        if re.fullmatch(r"conexao wi-?fi|wi-?fi embutido|suporte para o aplicativo smartthings", fold(label)):
            answers.append((yes_no(value), f"{sec} > {label} = {value}"))
    for flag, evidence in answers:
        if flag:
            return True, evidence
    return (False, answers[0][1]) if answers else (None, None)


_FEATURE_SECTIONS = ("recursos", "smart", "conectividade")


def _pod_features(rows, english: dict[str, str]) -> list[str]:
    """English names of the features the table says are present ('Sim')."""
    out = []
    for sec, label, value in rows:
        if fold(sec).startswith(_FEATURE_SECTIONS) and fold(value) == "sim":
            out.append(english.get(label, label))
    return list(dict.fromkeys(out))


def _table(rows, labels: dict[str, str], values: list[str]) -> dict[str, str]:
    """{'Section > Label': English value} for EVERY row (a repeated key keeps all values, ' | '-joined)."""
    table: dict[str, str] = {}
    for (sec, label, _), val in zip(rows, values):
        key = f"{labels[sec]} > {labels[label]}" if sec else labels[label]
        table[key] = f"{table[key]} | {val}" if key in table else val
    return table


def build_record(url: str, ld: dict, rows, card: dict, manuals: list[dict]) -> tuple[ProductRecord, list[tuple[str, str]]]:
    """Pure function: parsed PDP + spec rows + card API model -> ProductRecord (+ [(doc_type, https url)] to download)."""
    code = ld.get("sku") or card.get("modelCode") or ""
    model = _model(code)
    name = clean(ld.get("name") or card.get("displayName") or model)
    slug = (urlsplit(url).path.rstrip("/").rsplit("/", 1) + [""])[-1]
    sub = classify(name, code, slug, card.get("pviSubtypeName") or "")
    if sub is None:
        raise SamsungBrPageError(f"{url} is not a supported cooking product (hood, kit/bundle or unknown type)")
    prices = _price_from_model(card)
    sig = _signals(card)

    oven_l = pt_number(_pick(rows, r"capacidade do forno") or _pick(rows, r"capacidade", r"pesos|capacidade"))
    w, h, d = _dimensions(rows)
    weight_kg = pt_number(_pick(rows, r"peso \(liquido\)|peso liquido"))
    volt, hz = _electrical(rows)
    burners = _pick(rows, r"numero de queimadores") or _pick(rows, r"cooktop", r"cooktop")
    n_burners = re.search(r"\d+", burners or "")
    burner_rows = [(l, v) for _, l, v in rows if re.fullmatch(r"queimador( \d+| - .+)", fold(l))]
    wifi_ok, wifi_ev = _wifi(rows)
    finish_pt = _pick(rows, r"cor do forno|cor", r"materiais")

    labels_pt = list(dict.fromkeys(t for sec, label, _ in rows for t in (sec, label) if t))
    labels = dict(zip(labels_pt, translate_many(labels_pt, "label")))
    values = translate_many([v for _, _, v in rows], "value")
    table = _table(rows, labels, values)
    english_finish = translate_many([finish_pt], "value")[0] if finish_pt else None

    std: dict[str, str] = {"Fuel": _SUB_FUEL.get(sub, "")} if sub in _SUB_FUEL else {}
    if oven_l and sub != "gas_cooktop":
        std["Oven capacity (L)"] = fmt(oven_l)
        std["Oven capacity (cu ft)"] = fmt(round(units.l_to_cuft(oven_l), 2))
    if n_burners and sub in ("gas_oven", "gas_cooktop"):
        std["Burners/elements"] = n_burners.group(0)
    if burner_rows:
        std["Burner/element detail"] = "; ".join(f"{translate_many([l], 'label')[0]}: {translate_many([v], 'value')[0]}"
                                                 for l, v in burner_rows)
    for key, val in zip(("Width (mm)", "Height (mm)", "Depth (mm)"), _dimensions(rows, 4)):
        if val is not None:
            std[key] = fmt(round(units.in_to_mm(val)))
    if weight_kg:
        std["Weight (kg)"] = fmt(weight_kg)
    if prices["regular"]:
        std["List price (BRL)"] = fmt(prices["regular"])
    if prices["cash"] and prices["regular"] and prices["cash"] < prices["regular"]:
        std["Cash price excluded (BRL)"] = fmt(prices["cash"])
    if prices["original"] and prices["original"] != prices["regular"]:
        std["Struck-through original price (BRL)"] = fmt(prices["original"])
    std["Price basis"] = ("samsung.com/br regular price (installment price without the a-vista cash discount); "
                          "excludes cash/PIX/card discount prices")
    std["Source language"] = "pt-BR (labels/values translated; originals in RawSpec)"

    docs = []
    for i, man in enumerate(manuals[:MAX_PDFS]):
        docs.append(("Manual" if i == 0 else "Manual EN" if man["lang"].startswith("ingl") else "Manual 2", man["url"]))
    image = _abs(ld.get("image") if isinstance(ld.get("image"), str) else None)
    return ProductRecord(
        brand=BRAND, model_number=model, product_name=name, product_url=url, category="cooking", subcategory=sub,
        finish_color=english_finish, region=REGION, country=COUNTRY, currency=CURRENCY, price_usd=None,
        price_local=prices["regular"],
        capacity_total_cuft=round(units.l_to_cuft(oven_l), 2) if oven_l and sub != "gas_cooktop" else None,
        width_in=w, height_in=h, depth_in=d,
        weight_lb=None if not weight_kg else round(units.kg_to_lb(weight_kg), 1),
        voltage_v=volt, frequency_hz=hz, energy_kwh_year=None, energy_star=None,
        wifi_supported=wifi_ok, wifi_evidence=wifi_ev,
        pod_features=_pod_features(rows, labels),
        extra_specs={**table, **std}, image_url=image, **sig,
    ), docs


def _card_model(payload: dict, code: str) -> dict:
    """The model dict for `code` out of a card/detail payload ({} when the site returns none)."""
    try:
        for fam in payload["response"]["resultData"]["productList"]:
            for m in fam.get("modelList") or []:
                if _model(m.get("modelCode") or "") == _model(code):
                    return m
    except (KeyError, TypeError):
        pass
    return {}


def scrape(url: str) -> tuple[ProductRecord, list[DocumentRecord], list[RawSpec]]:
    _require_br(url)
    with _net() as net:
        page_html = net.text(url, as_page=True, ok=lambda t: "pdd32-product-spec" in t or '"sku"' in t)
        ld = parse_product_ld(page_html)
        rows = parse_spec_rows(page_html)
        if not ld.get("sku") or not rows:
            raise SamsungBrPageError(f"{url}: no product data (sku / spec table) found on the page")
        card_url = (f"{CARD_URL}?siteCode=br&modelList={ld['sku']}&saleSkuYN=N&onlyRequestSkuYN=N&keySummaryYN=N"
                    "&keySpecYN=N&quicklookYN=N")
        try:
            card = _card_model(json.loads(net.text(card_url, ok=lambda t: t.lstrip().startswith("{"))), ld["sku"])
        except (SamsungBrPageError, ValueError) as exc:
            print(f"samsung_br: price block unavailable for {ld['sku']} ({exc}); price_local left empty", file=sys.stderr)
            card = {}
    product, doc_urls = build_record(url, ld, rows, card, parse_manuals(page_html))
    fname_model = _safe_model(product.model_number)
    docs = []
    for doc_type, doc_url in doc_urls:
        time.sleep(MIN_DELAY_S)
        d = download_pdf(BRAND, fname_model, doc_type, doc_url)
        if d:
            docs.append(d)
    raw = [RawSpec(brand=BRAND, model_number=product.model_number, source="web", section=s, key=k, value=v)
           for s, k, v in rows]
    return product, docs, raw
