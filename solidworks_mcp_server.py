# -*- coding: utf-8 -*-
"""
MCP-сервер для SolidWorks 2023/2024 — листовой металл, сборки, развёртки.
Автор: Claude (CAD In). Версия 0.4.

Изменения 0.4 (05.09.2026, после падения "MCP solidworks: Server disconnected"):
  * журнал server.log рядом со скриптом: старт/стоп, все ошибки, таймауты —
    теперь причину обрыва видно постфактум (stdout занят протоколом MCP,
    поэтому пишем только в файл и stderr);
  * faulthandler → server_crash.log: ловит жёсткий сбой COM/SolidWorks
    (Access Violation), после которого python умирает без traceback;
  * COM_TIMEOUT 55 с: раньше вызов на модальном окне SolidWorks висел вечно,
    и клиент убивал сервер. Теперь возвращается понятная ошибка, процесс жив;
  * повторный вызов при занятом SolidWorks не встаёт в очередь, а сразу
    объясняет, что нужно закрыть диалог;
  * COM-поток поднимается заново, если умер; _run ловит BaseException.

Изменения 0.3 (31.08.2026, по итогам живой отладки):
  * _get(): проверки callable() было недостаточно. Члены, возвращающие
    COM-объект (FirstFeature, GetNextFeature, CreateMassProperty, GetDefinition,
    GetBodies2 и др.), сами проходят callable(), и попытка их вызвать давала
    "Член группы не найден". Добавлен явный список _AS_PROPERTY плюс откат
    к значению при COM-ошибке. Это чинило get_feature_tree, get_mass_properties
    и get_sheet_metal_info;
  * save_document: Extension.SaveAs отвергал None в параметре ExportData
    ("Несовпадение типов", аргумент 4) — передаём VARIANT(VT_DISPATCH, None)
    и идём каскадом Extension.SaveAs → SaveAs3 → SaveAs2 → SaveAs, возвращая
    сработавший вариант; Save3 получил откат на Save().

Изменения 0.2 (по итогам живой отладки на SW2024 + Python 3.14):
  * _get(): COM отдаёт часть методов без аргументов как свойства — универсальная обёртка;
  * SelectByID2: Callout передаётся как VARIANT(VT_DISPATCH, None), координаты — float;
  * шаблоны документов: GetUserPreferenceStringValue может быть пуст — добавлен
    GetDocumentTemplate + проверка файла + поиск шаблонов на диске;
  * InsertSheetMetalBaseFlange2: рабочая сигнатура на SW2024 — 19 аргументов;
  * FeatureCut4: рабочая сигнатура на SW2024 — 27 аргументов;
  * export_flat_pattern_dxf: требует сохранённую деталь (иначе SolidWorks
    открывает модальный диалог сохранения и COM-вызов зависает).

Назначение: даёт Claude (Claude Desktop / Cowork) набор инструментов для
проектирования НАПРЯМУЮ в открытом SolidWorks через COM API (pywin32).

Специализация:
  * листовые детали: базовая кромка, эскизы, вырезы;
  * сборки: вставка компонентов, сопряжения (совпадение/концентричность/расстояние);
  * развёртки: экспорт в DXF;
  * чтение и правка существующего: дерево элементов, размеры, перестроение;
  * скриншот модели — чтобы ИИ ВИДЕЛ, что получилось;
  * execute_python — аварийный люк: выполнение произвольного COM-кода,
    если готового инструмента не хватает или сигнатура метода отличается
    в вашей версии SolidWorks.

ВАЖНО ПРО ЕДИНИЦЫ: SolidWorks API работает в МЕТРАХ и радианах независимо
от настроек документа. Все инструменты сервера принимают МИЛЛИМЕТРЫ и
градусы и переводят их внутри (мм / 1000 → м).

Запуск: сервер запускается Клодом автоматически (stdio) — см. README.
Требования: Windows, SolidWorks запущен, Python 3.10+,
  pip install mcp pywin32 pillow
"""

import io
import os
import sys
import json
import time
import tempfile
import traceback
import contextlib
import threading
import faulthandler
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as _FutTimeout

# ---------------------------------------------------------------------------
# ДИАГНОСТИКА (v0.4). Причина "MCP solidworks: Server disconnected" — это всегда
# смерть ЭТОГО процесса. Три главных сценария: (1) жёсткий сбой в COM/SolidWorks
# (Access Violation — питон умирает без traceback), (2) зависание на модальном
# окне SolidWorks: клиент не дожидается ответа и убивает сервер, (3) мусор в
# stdout, который ломает JSON-RPC. Ниже — журнал, страховка от (1) и (2).
# ВАЖНО: stdout занят протоколом MCP, поэтому пишем ТОЛЬКО в файл и stderr.
# ---------------------------------------------------------------------------
_LOG_DIR = os.path.dirname(os.path.abspath(__file__))
_LOG_PATH = os.path.join(_LOG_DIR, "server.log")
_CRASH_PATH = os.path.join(_LOG_DIR, "server_crash.log")
_log_lock = threading.Lock()


def _log(msg):
    """Строка в server.log рядом со скриптом (stdout трогать нельзя)."""
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n"
    try:
        with _log_lock:
            with open(_LOG_PATH, "a", encoding="utf-8") as f:
                f.write(line)
                f.flush()
    except Exception:
        pass
    try:
        sys.stderr.write(line)
        sys.stderr.flush()
    except Exception:
        pass


try:
    # Access Violation в SolidWorks/COM убивает процесс молча. faulthandler
    # успевает записать стек в файл — потом видно, на каком вызове упало.
    _crash_file = open(_CRASH_PATH, "a", buffering=1, encoding="utf-8")
    faulthandler.enable(file=_crash_file, all_threads=True)
except Exception:
    pass


def _thread_hook(args):
    _log(f"НЕПЕРЕХВАЧЕННОЕ исключение в потоке {args.thread_name if args.thread else '?'}: "
         f"{args.exc_type.__name__}: {args.exc_value}")


try:
    threading.excepthook = _thread_hook
except Exception:
    pass

sys.excepthook = lambda t, v, tb: _log("НЕПЕРЕХВАЧЕННОЕ исключение: " +
                                       "".join(traceback.format_exception(t, v, tb)))

# ---------------------------------------------------------------------------
# COM работает в однопоточном апартаменте (STA): все обращения к SolidWorks
# выполняем в ОДНОМ выделенном потоке, иначе pywin32 даёт случайные ошибки.
# ---------------------------------------------------------------------------
_com_executor: "ThreadPoolExecutor | None" = None
_sw = {"app": None}  # кэш COM-объекта приложения (живёт только в COM-потоке)
_pending = {"fut": None, "started": 0.0, "what": ""}

# Сколько ждать ответа SolidWorks. Мост Claude обрывает вызов на 60 с, поэтому
# отвечаем понятной ошибкой чуть раньше — так процесс остаётся живым.
COM_TIMEOUT = 55


def _com_init():
    """Инициализация COM в выделенном потоке."""
    import pythoncom
    pythoncom.CoInitialize()


def _ensure_executor():
    """Поток COM жив? Если нет — поднять заново (сервер переживает сбой потока)."""
    global _com_executor
    dead = (_com_executor is None) or getattr(_com_executor, "_shutdown", False)
    if dead:
        _com_executor = ThreadPoolExecutor(max_workers=1, initializer=_com_init,
                                           thread_name_prefix="sw-com")
        _log("COM-поток создан заново")
    return _com_executor


def com_call(fn, *args, **kwargs):
    """Выполнить функцию в COM-потоке. Не даём процессу зависнуть навсегда:
    при таймауте возвращаем ошибку, а не молчим до убийства сервера клиентом."""
    prev = _pending["fut"]
    if prev is not None and not prev.done():
        waited = round(time.time() - _pending["started"])
        raise RuntimeError(
            f"SolidWorks всё ещё занят предыдущей операцией ({_pending['what']}, "
            f"{waited} с). Почти всегда это ОТКРЫТОЕ МОДАЛЬНОЕ ОКНО в SolidWorks "
            f"(сохранение, перестроение, сообщение об ошибке). Закройте окно в "
            f"SolidWorks — и повторите команду.")
    ex = _ensure_executor()
    fut = ex.submit(fn, *args, **kwargs)
    _pending.update({"fut": fut, "started": time.time(),
                     "what": getattr(fn, "__qualname__", "операция")})
    try:
        return fut.result(timeout=COM_TIMEOUT)
    except _FutTimeout:
        _log(f"ТАЙМАУТ {COM_TIMEOUT}s: {_pending['what']}")
        raise RuntimeError(
            f"SolidWorks не ответил за {COM_TIMEOUT} с. Обычно это модальное окно, "
            f"ждущее нажатия кнопки. Посмотрите на окно SolidWorks, закройте диалог "
            f"и повторите. Сервер продолжает работать.")


# ---------------------------------------------------------------------------
# Вспомогательные функции (вызываются ТОЛЬКО из COM-потока)
# ---------------------------------------------------------------------------

def _app():
    """Подключиться к запущенному SolidWorks (или запустить новый экземпляр)."""
    import win32com.client
    if _sw["app"] is not None:
        try:
            _ = _sw["app"].Visible  # проверка, что COM-ссылка ещё жива
            return _sw["app"]
        except Exception:
            _sw["app"] = None
    app = win32com.client.Dispatch("SldWorks.Application")
    app.Visible = True
    _sw["app"] = app
    return app


def _model(doc_type=None):
    """Активный документ с проверкой. doc_type: 1=деталь, 2=сборка, 3=чертёж."""
    app = _app()
    model = app.ActiveDoc
    if model is None:
        raise RuntimeError("В SolidWorks нет активного документа. "
                           "Откройте или создайте документ (open_document / create_part / create_assembly).")
    if doc_type is not None and _get(model, "GetType") != doc_type:
        names = {1: "деталь (.sldprt)", 2: "сборка (.sldasm)", 3: "чертёж (.slddrw)"}
        raise RuntimeError(f"Активный документ не того типа: нужен {names.get(doc_type)}, "
                           f"а активен тип {_get(model, 'GetType')}.")
    return model


def _byref_i4(value=0):
    """VARIANT byref для out-параметров COM (Errors/Warnings и т.п.)."""
    import pythoncom
    from win32com.client import VARIANT
    return VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, value)


def _mm(v):
    """мм → м (API SolidWorks всегда в метрах)."""
    return float(v) / 1000.0


# Члены COM, которые pywin32 отдаёт как СВОЙСТВА: их нельзя вызывать со скобками.
# Отдельная опасность — те, что возвращают COM-объект (Feature, MassProperty,
# Body): сам объект тоже проходит проверку callable(), и попытка его вызвать
# даёт "Член группы не найден" (MEMBERNOTFOUND). Поэтому нужен явный список,
# а не только callable(). Проверено вживую на SW2024 SP5 + Python 3.14 + pywin32-312.
_AS_PROPERTY = {
    # возвращают COM-объект — именно они ломались
    "FirstFeature", "GetNextFeature", "GetFirstDocument", "GetNext",
    "CreateMassProperty", "GetBodies2", "GetDefinition", "GetSpecificFeature2",
    # возвращают скаляр
    "GetTitle", "GetPathName", "GetType", "GetTypeName2", "Name", "Name2",
    "IsSuppressed", "EditRebuild3", "ForceRebuild3", "ViewZoomtofit2",
}


def _get(obj, name, *args):
    """Вызов COM-члена, который может быть методом ИЛИ свойством.

    В связке pywin32 + SolidWorks методы без аргументов резолвятся то как
    методы, то как свойства. Проверки callable() недостаточно: член вроде
    CreateMassProperty отдаёт COM-объект, который сам по себе callable, и
    вызов такого объекта падает с MEMBERNOTFOUND. Порядок разбора:
      1) есть аргументы  → это точно метод, вызываем;
      2) имя в _AS_PROPERTY → берём значение, НЕ вызывая;
      3) иначе callable  → пробуем вызвать, при COM-ошибке откатываемся к значению.
    """
    attr = getattr(obj, name)
    if args:
        return attr(*args)
    if name in _AS_PROPERTY:
        return attr
    if callable(attr):
        try:
            return attr()
        except Exception:
            return attr
    return attr


def _nothing():
    """VARIANT(VT_DISPATCH, None) — 'Nothing' для параметра Callout в SelectByID2.
    Обычный None вызывает 'Несовпадение типов'."""
    import pythoncom
    from win32com.client import VARIANT
    return VARIANT(pythoncom.VT_DISPATCH, None)


def _template_path(app, doc_type):
    """Путь к шаблону документа. doc_type: 1=деталь, 2=сборка, 3=чертёж.
    Порядок: настройка по умолчанию → GetDocumentTemplate → поиск на диске."""
    pref = {1: 8, 2: 9, 3: 10}[doc_type]   # swDefaultTemplatePart/Assembly/Drawing
    tpl = app.GetUserPreferenceStringValue(pref)
    if tpl and os.path.isfile(tpl):
        return tpl
    tpl = app.GetDocumentTemplate(doc_type, "", 0, 0.0, 0.0)
    if tpl and os.path.isfile(tpl):
        return tpl
    # шаблон из настроек не существует на диске — ищем сами
    ext = {1: ".prtdot", 2: ".asmdot", 3: ".drwdot"}[doc_type]
    roots = [r"C:\ProgramData\SolidWorks", r"C:\Program Files\SOLIDWORKS Corp"]
    candidates = []
    for root in roots:
        for dirpath, _dirs, files in os.walk(root):
            for f in files:
                if f.lower().endswith(ext):
                    candidates.append(os.path.join(dirpath, f))
    if not candidates:
        raise RuntimeError(f"Не найден ни один шаблон {ext}. Укажите шаблон в "
                           f"Настройки → Параметры системы → Шаблоны по умолчанию.")
    # предпочитаем ГОСТ-шаблон, если есть
    for c in candidates:
        if "gost" in os.path.basename(c).lower():
            return c
    return candidates[0]


def _select(model, name, obj_type, x=0.0, y=0.0, z=0.0, append=False, mark=0):
    """Обёртка над SelectByID2 с проверкой результата."""
    ok = model.Extension.SelectByID2(name, obj_type, float(x), float(y), float(z),
                                     append, mark, _nothing(), 0)
    if not ok:
        raise RuntimeError(f"SelectByID2 не смог выбрать объект: имя='{name}', тип='{obj_type}'. "
                           f"Проверьте точное имя (в русской версии SolidWorks имена русские: "
                           f"'Спереди', 'Эскиз1', 'Деталь1-1@Сборка1' и т.д.)")
    return True


def _feature_names(model, type_filter=None, limit=200):
    """Обход дерева элементов: [(имя, тип), ...]."""
    out = []
    feat = _get(model, "FirstFeature")
    while feat is not None and len(out) < limit:
        t = _get(feat, "GetTypeName2")
        if type_filter is None or t == type_filter:
            out.append((_get(feat, "Name"), t))
        feat = _get(feat, "GetNextFeature")
    return out


def _ok(**kw):
    """Единый формат успешного ответа."""
    kw.setdefault("status", "ok")
    return json.dumps(kw, ensure_ascii=False, indent=1)


# ---------------------------------------------------------------------------
# MCP-сервер
# Совместимость: в пакете mcp 2.x класс называется MCPServer, в 1.x — FastMCP
# ---------------------------------------------------------------------------
try:
    from mcp.server.mcpserver import MCPServer as _ServerClass, Image  # mcp >= 2.0
except ImportError:
    from mcp.server.fastmcp import FastMCP as _ServerClass, Image      # mcp 1.x

mcp = _ServerClass(
    "solidworks-sheetmetal",
    instructions=(
        "Инструменты управления SolidWorks (листовой металл, сборки, развёртки). "
        "Все размеры — в МИЛЛИМЕТРАХ, углы — в градусах. "
        "Перед созданием элементов делай screenshot, чтобы видеть результат. "
        "Если готовый инструмент падает из-за сигнатуры API конкретной версии SW — "
        "используй execute_python и официальную справку help.solidworks.com."
    ),
)


# ---------------------------------------------------------------------------
# ЛОГ ВЫЗОВОВ ИНСТРУМЕНТОВ — для панели в ролике (calls.jsonl)
# Каждый вызов пишется одной строкой JSON: время, инструмент, аргументы,
# длительность, ok/ошибка. Путь можно переопределить переменной окружения
# SW_MCP_CALL_LOG. Отключить: SW_MCP_CALL_LOG=off
# ---------------------------------------------------------------------------
import time as _time
import inspect as _inspect
import functools as _functools

_CALL_LOG = os.environ.get(
    "SW_MCP_CALL_LOG",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "calls.jsonl"),
)


def _log_write(rec):
    if _CALL_LOG.lower() == "off":
        return
    try:
        with open(_CALL_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _short_args(fn, a, kw, limit=80):
    try:
        b = _inspect.signature(fn).bind_partial(*a, **kw)
        out = {}
        for k, v in b.arguments.items():
            s = v if isinstance(v, (int, float, bool)) or v is None else str(v)
            if isinstance(s, str) and len(s) > limit:
                s = s[:limit] + "…"
            out[k] = s
        return out
    except Exception:
        return {}


_orig_tool = mcp.tool


def _logged_tool(*dargs, **dkwargs):
    deco = _orig_tool(*dargs, **dkwargs)

    def wrap(fn):
        @_functools.wraps(fn)
        def inner(*a, **kw):
            t0 = _time.time()
            ok, err = True, None
            try:
                res = fn(*a, **kw)
                if isinstance(res, str) and res.lstrip().startswith("{"):
                    try:
                        d = json.loads(res)
                        if d.get("status") == "error":
                            ok, err = False, d.get("error")
                    except Exception:
                        pass
                return res
            except Exception as e:
                ok, err = False, str(e)
                raise
            finally:
                _log_write({
                    "t": round(t0, 3),
                    "ts": _time.strftime("%Y-%m-%d %H:%M:%S", _time.localtime(t0)),
                    "dur": round(_time.time() - t0, 3),
                    "tool": fn.__name__,
                    "args": _short_args(fn, a, kw),
                    "ok": ok,
                    "error": err,
                })
        return deco(inner)
    return wrap


mcp.tool = _logged_tool
_log_write({"t": round(_time.time(), 3),
            "ts": _time.strftime("%Y-%m-%d %H:%M:%S"),
            "tool": "__server_start__", "args": {}, "ok": True, "error": None})


# ============================= ПОДКЛЮЧЕНИЕ =================================

@mcp.tool()
def connect_solidworks() -> str:
    """Подключиться к запущенному SolidWorks и вернуть информацию о нём."""
    def job():
        app = _app()
        ver = app.RevisionNumber          # например "31.x" = SW2023, "32.x" = SW2024
        model = app.ActiveDoc
        active = _get(model, "GetTitle") if model is not None else None
        return _ok(revision=str(ver),
                   hint="Revision 31=SW2023, 32=SW2024, 33=SW2025",
                   active_document=active)
    return _run(job)


@mcp.tool()
def list_open_documents() -> str:
    """Список открытых в SolidWorks документов."""
    def job():
        app = _app()
        docs = []
        d = _get(app, "GetFirstDocument")
        while d is not None:
            docs.append({"title": _get(d, "GetTitle"), "path": _get(d, "GetPathName"),
                         "type": {1: "part", 2: "assembly", 3: "drawing"}.get(_get(d, "GetType"), "?")})
            d = _get(d, "GetNext")
        return _ok(documents=docs)
    return _run(job)


# ============================= ДОКУМЕНТЫ ===================================

@mcp.tool()
def create_part() -> str:
    """Создать новую деталь по шаблону по умолчанию."""
    def job():
        app = _app()
        tpl = _template_path(app, 1)
        doc = app.NewDocument(tpl, 0, 0.0, 0.0)
        if doc is None:
            raise RuntimeError(f"NewDocument вернул None (шаблон: {tpl}).")
        return _ok(created=_get(doc, "GetTitle"), template=tpl)
    return _run(job)


@mcp.tool()
def create_assembly() -> str:
    """Создать новую сборку по шаблону по умолчанию."""
    def job():
        app = _app()
        tpl = _template_path(app, 2)
        doc = app.NewDocument(tpl, 0, 0.0, 0.0)
        if doc is None:
            raise RuntimeError(f"NewDocument вернул None (шаблон: {tpl}).")
        return _ok(created=_get(doc, "GetTitle"), template=tpl)
    return _run(job)


@mcp.tool()
def open_document(path: str) -> str:
    """Открыть документ SolidWorks по полному пути (.sldprt / .sldasm / .slddrw)."""
    def job():
        app = _app()
        if not os.path.isfile(path):
            raise RuntimeError(f"Файл не найден: {path}")
        ext = os.path.splitext(path)[1].lower()
        dtype = {".sldprt": 1, ".sldasm": 2, ".slddrw": 3}.get(ext)
        if dtype is None:
            raise RuntimeError(f"Неизвестное расширение: {ext}")
        errs, warns = _byref_i4(), _byref_i4()
        doc = app.OpenDoc6(path, dtype, 1, "", errs, warns)   # 1 = silent
        if doc is None:
            raise RuntimeError(f"OpenDoc6 не открыл файл (errors={errs.value}, warnings={warns.value}).")
        return _ok(opened=_get(doc, "GetTitle"), errors=errs.value, warnings=warns.value)
    return _run(job)


@mcp.tool()
def save_document(save_as_path: str = "") -> str:
    """Сохранить активный документ. save_as_path — сохранить как (полный путь);
    расширение задаёт формат: .sldprt/.sldasm — родной, .step/.stp, .dxf, .pdf, .stl — экспорт."""
    def job():
        model = _model()
        if not save_as_path:
            errs, warns = _byref_i4(), _byref_i4()
            if not _get(model, "GetPathName"):
                raise RuntimeError("Документ ещё ни разу не сохранялся — укажите save_as_path "
                                   "(полный путь с именем файла), иначе SolidWorks откроет "
                                   "модальный диалог и вызов зависнет.")
            # Save3 на части сборок SW2024 падает с "Несовпадение типов" —
            # откатываемся на простую форму Save().
            try:
                ok = model.Save3(1, errs, warns)   # 1 = swSaveAsOptions_Silent
            except Exception:
                ok = model.Save()
            if not ok:
                raise RuntimeError(f"Сохранение вернуло ошибку (errors={errs.value}).")
            return _ok(saved=_get(model, "GetPathName") or _get(model, "GetTitle"))

        # Сохранить как / экспорт.
        # Extension.SaveAs на SW2024 отвергает None в параметре ExportData
        # ("Несовпадение типов", аргумент 4) — передаём VARIANT(VT_DISPATCH, None).
        # Сигнатуры между версиями различаются, поэтому идём каскадом и
        # сообщаем, какой вариант сработал.
        attempts = []
        for variant, call in (
            ("Extension.SaveAs", lambda: model.Extension.SaveAs(
                save_as_path, 0, 1, _nothing(), _byref_i4(), _byref_i4())),
            ("SaveAs3", lambda: model.SaveAs3(save_as_path, 0, 0)),
            ("SaveAs2", lambda: model.SaveAs2(save_as_path, 0, True, False)),
            ("SaveAs",  lambda: model.SaveAs(save_as_path)),
        ):
            try:
                ok = call()
            except Exception as exc:
                attempts.append(f"{variant}: {exc}")
                continue
            if ok:
                return _ok(saved=save_as_path, variant=variant)
            attempts.append(f"{variant}: вернул False")
        raise RuntimeError(
            "Ни один вариант сохранения не сработал. Проверьте, что папка существует, "
            "файл не открыт в другой программе и расширение поддерживается. Попытки: "
            + " | ".join(attempts))
    return _run(job)


@mcp.tool()
def close_document(title: str = "") -> str:
    """Закрыть документ по заголовку (пусто — активный). НЕ сохраняет автоматически."""
    def job():
        app = _app()
        t = title
        if not t:
            model = app.ActiveDoc
            if model is None:
                raise RuntimeError("Нет активного документа.")
            t = _get(model, "GetTitle")
        app.CloseDoc(t)
        return _ok(closed=t)
    return _run(job)


# ======================= ОБРАТНАЯ СВЯЗЬ ДЛЯ ИИ =============================

@mcp.tool()
def screenshot(width: int = 1000, height: int = 750, zoom_to_fit: bool = True,
               isometric: bool = False) -> Image:
    """Скриншот активной модели (текущий вид). Используй после каждой операции,
    чтобы видеть результат построения."""
    def job():
        model = _model()
        if isometric:
            for view_name in ("*Изометрия", "*Isometric"):
                try:
                    model.ShowNamedView2(view_name, 7)
                    break
                except Exception:
                    continue
        if zoom_to_fit:
            _get(model, "ViewZoomtofit2")
        tmp_bmp = os.path.join(tempfile.gettempdir(), "sw_mcp_view.bmp")
        ok = model.SaveBMP(tmp_bmp, int(width), int(height))
        if not ok or not os.path.isfile(tmp_bmp):
            raise RuntimeError("SaveBMP не смог сохранить изображение.")
        from PIL import Image as PILImage
        tmp_png = os.path.join(tempfile.gettempdir(), "sw_mcp_view.png")
        PILImage.open(tmp_bmp).save(tmp_png, "PNG")
        with open(tmp_png, "rb") as f:
            return f.read()
    data = com_call(job)
    return Image(data=data, format="png")


@mcp.tool()
def get_feature_tree(limit: int = 100) -> str:
    """Дерево элементов активного документа: имена и типы (для чтения модели)."""
    def job():
        model = _model()
        feats = _feature_names(model, limit=limit)
        return _ok(features=[{"name": n, "type": t} for n, t in feats])
    return _run(job)


@mcp.tool()
def get_mass_properties() -> str:
    """Масса (кг), объём (м3), площадь (м2), центр масс (мм) активной детали/сборки."""
    def job():
        model = _model()
        mp = _get(model.Extension, "CreateMassProperty")
        if mp is None:
            raise RuntimeError("CreateMassProperty вернул None.")
        com = list(mp.CenterOfMass)
        return _ok(mass_kg=round(mp.Mass, 6),
                   volume_m3=mp.Volume,
                   surface_area_m2=mp.SurfaceArea,
                   center_of_mass_mm=[round(c * 1000, 3) for c in com])
    return _run(job)


@mcp.tool()
def get_sheet_metal_info() -> str:
    """Параметры листового металла активной детали: толщина и радиус сгиба (мм)."""
    def job():
        model = _model(1)
        feat = _get(model, "FirstFeature")
        while feat is not None:
            if _get(feat, "GetTypeName2") == "SheetMetal":
                data = _get(feat, "GetDefinition")
                if data is None:
                    raise RuntimeError("GetDefinition вернул None для элемента SheetMetal.")
                return _ok(thickness_mm=round(data.Thickness * 1000, 4),
                           bend_radius_mm=round(data.BendRadius * 1000, 4),
                           feature=_get(feat, "Name"))
            feat = _get(feat, "GetNextFeature")
        raise RuntimeError("В детали нет элемента 'Листовой металл' (SheetMetal). "
                           "Это не листовая деталь, либо она построена иначе.")
    return _run(job)


@mcp.tool()
def get_dimension(name: str) -> str:
    """Прочитать размер по имени, например 'D1@Эскиз1' или 'D1@Базовая-кромка1'.
    Возвращает значение в мм (для угловых размеров — в градусах, см. поле unit_note)."""
    def job():
        model = _model()
        dim = model.Parameter(name)
        if dim is None:
            raise RuntimeError(f"Размер '{name}' не найден. Формат: 'D1@Эскиз1'. "
                               f"Имена смотри в get_feature_tree.")
        return _ok(name=name, value_mm=round(dim.SystemValue * 1000, 6),
                   unit_note="SystemValue в метрах, здесь переведено в мм; "
                             "для углов SystemValue в радианах — тогда value_mm некорректно, "
                             "используй execute_python.")
    return _run(job)


@mcp.tool()
def set_dimension(name: str, value_mm: float, rebuild: bool = True) -> str:
    """Изменить размер по имени (значение в мм) и перестроить модель."""
    def job():
        model = _model()
        dim = model.Parameter(name)
        if dim is None:
            raise RuntimeError(f"Размер '{name}' не найден.")
        old = dim.SystemValue
        dim.SystemValue = _mm(value_mm)   # мм → м
        if rebuild:
            if not _get(model, "EditRebuild3"):
                raise RuntimeError("EditRebuild3: перестроение с ошибками — проверьте модель.")
        return _ok(name=name, old_mm=round(old * 1000, 4), new_mm=value_mm)
    return _run(job)


@mcp.tool()
def rebuild(force: bool = False) -> str:
    """Перестроить модель (force=true — принудительно всю)."""
    def job():
        model = _model()
        ok = model.ForceRebuild3(False) if force else _get(model, "EditRebuild3")
        if not ok:
            raise RuntimeError("Перестроение завершилось с ошибками (см. дерево элементов).")
        return _ok(rebuilt=True, forced=force)
    return _run(job)


# ============================== ЭСКИЗЫ =====================================

@mcp.tool()
def create_sketch(plane_name: str = "Спереди") -> str:
    """Начать эскиз на плоскости ('Спереди'/'Сверху'/'Справа' или 'Front Plane'...
    в английской версии; можно имя любой плоскости/грани из дерева).

    Оси эскиза (проверено на SW2024): 'Спереди' — x=X, y=Y модели;
    'Сверху' — x=X, y = МИНУС Z модели (отверстие в точке модели (x,z)
    рисуй в эскизе как (x, -z))."""
    def job():
        model = _model(1)
        _select(model, plane_name, "PLANE")
        model.SketchManager.InsertSketch(True)
        sk = model.SketchManager.ActiveSketch
        if sk is None:
            raise RuntimeError("Не удалось открыть эскиз.")
        return _ok(sketch=_get(sk, "Name"),
                   note="Рисуй sketch_circle / sketch_rectangle / sketch_line, затем close_sketch.")
    return _run(job)


@mcp.tool()
def sketch_rectangle(x1_mm: float, y1_mm: float, x2_mm: float, y2_mm: float) -> str:
    """Прямоугольник по двум углам (мм) в активном эскизе."""
    def job():
        model = _model(1)
        if model.SketchManager.ActiveSketch is None:
            raise RuntimeError("Нет активного эскиза — сначала create_sketch.")
        segs = model.SketchManager.CreateCornerRectangle(_mm(x1_mm), _mm(y1_mm), 0,
                                                         _mm(x2_mm), _mm(y2_mm), 0)
        if segs is None:
            raise RuntimeError("CreateCornerRectangle вернул None.")
        return _ok(created="rectangle")
    return _run(job)


@mcp.tool()
def sketch_circle(xc_mm: float, yc_mm: float, radius_mm: float) -> str:
    """Окружность: центр (мм) и радиус (мм) в активном эскизе."""
    def job():
        model = _model(1)
        if model.SketchManager.ActiveSketch is None:
            raise RuntimeError("Нет активного эскиза — сначала create_sketch.")
        seg = model.SketchManager.CreateCircleByRadius(_mm(xc_mm), _mm(yc_mm), 0, _mm(radius_mm))
        if seg is None:
            raise RuntimeError("CreateCircleByRadius вернул None.")
        return _ok(created="circle")
    return _run(job)


@mcp.tool()
def sketch_line(x1_mm: float, y1_mm: float, x2_mm: float, y2_mm: float) -> str:
    """Отрезок (мм) в активном эскизе. Для полилинии вызывай несколько раз подряд."""
    def job():
        model = _model(1)
        if model.SketchManager.ActiveSketch is None:
            raise RuntimeError("Нет активного эскиза — сначала create_sketch.")
        seg = model.SketchManager.CreateLine(_mm(x1_mm), _mm(y1_mm), 0, _mm(x2_mm), _mm(y2_mm), 0)
        if seg is None:
            raise RuntimeError("CreateLine вернул None.")
        return _ok(created="line")
    return _run(job)


@mcp.tool()
def close_sketch() -> str:
    """Завершить (закрыть) активный эскиз."""
    def job():
        model = _model(1)
        sk = model.SketchManager.ActiveSketch
        if sk is None:
            raise RuntimeError("Нет активного эскиза.")
        name = _get(sk, "Name")
        model.SketchManager.InsertSketch(True)
        return _ok(closed=name)
    return _run(job)


# ========================= ЛИСТОВОЙ МЕТАЛЛ =================================

@mcp.tool()
def create_base_flange(thickness_mm: float, bend_radius_mm: float = 1.0,
                       depth_mm: float = 0.0, sketch_name: str = "") -> str:
    """Базовая кромка (Base Flange) из эскиза — основа листовой детали.
    Замкнутый эскиз (прямоугольник) → плоский лист толщиной thickness_mm.
    Открытый профиль (полилиния) → гнутый профиль: укажи depth_mm (длина вытяжки)
    и bend_radius_mm. sketch_name — имя эскиза; пусто = последний эскиз.

    ВНИМАНИЕ: сигнатура InsertSheetMetalBaseFlange2 различается между версиями SW.
    Сервер пробует два известных варианта; если оба не сработают — вернёт ошибку
    с подсказкой (тогда используй execute_python + help.solidworks.com)."""
    def job():
        model = _model(1)
        # найти эскиз
        name = sketch_name
        if not name:
            sketches = _feature_names(model, "ProfileFeature")
            if not sketches:
                raise RuntimeError("В детали нет эскизов. Сначала create_sketch + геометрия + close_sketch.")
            name = sketches[-1][0]
        model.ClearSelection2(True)
        _select(model, name, "SKETCH")

        t = _mm(thickness_mm)
        r = _mm(bend_radius_mm)
        d = _mm(depth_mm) if depth_mm else 0.0
        fm = model.FeatureManager
        attempts = []
        # Вариант 1: 19 аргументов — ПРОВЕРЕНО ВЖИВУЮ на SW2024 и для открытого
        # (гнутый профиль), и для замкнутого (плоский лист) эскиза.
        # ВНИМАНИЕ: 18-й аргумент должен быть False — при True открытый профиль
        # схлопывается в один сгиб без полок (найдено сравнением с ручной операцией).
        try:
            feat = fm.InsertSheetMetalBaseFlange2(t, False, r, d, 0.0, False, 0, 0, 1,
                                                  _nothing(), False, False, 0.0001, 0.0001, 0.5,
                                                  True, False, False, True)
            if feat is not None:
                return _ok(feature=_get(feat, "Name"), variant="19args", thickness_mm=thickness_mm)
            attempts.append("вариант 19 аргументов: вернул None")
        except Exception as e:
            attempts.append(f"вариант 19 аргументов: {e}")
        # Вариант 2: короткая сигнатура (старые версии SW)
        try:
            feat = fm.InsertSheetMetalBaseFlange2(t, False, r, d, 0.0, False, 0, 0, 0, 0.0, 0.0, False)
            if feat is not None:
                return _ok(feature=_get(feat, "Name"), variant="12args", thickness_mm=thickness_mm)
            attempts.append("вариант 12 аргументов: вернул None")
        except Exception as e:
            attempts.append(f"вариант 12 аргументов: {e}")
        raise RuntimeError(
            "InsertSheetMetalBaseFlange2 не сработал ни с одной известной сигнатурой.\n"
            + "\n".join(attempts) +
            "\nПроверь точную сигнатуру для своей версии SW: help.solidworks.com → API Help → "
            "IFeatureManager::InsertSheetMetalBaseFlange2, затем вызови её через execute_python."
        )
    return _run(job)


@mcp.tool()
def cut_through(sketch_name: str = "") -> str:
    """Сквозной вырез по эскизу (отверстия, пазы) в листовой/обычной детали.
    sketch_name — имя эскиза с контуром выреза; пусто = последний эскиз.
    Пробует FeatureCut3/FeatureCut4 (сигнатуры различаются между версиями SW)."""
    def job():
        model = _model(1)
        name = sketch_name
        if not name:
            sketches = _feature_names(model, "ProfileFeature")
            if not sketches:
                raise RuntimeError("Нет эскизов для выреза.")
            name = sketches[-1][0]
        model.ClearSelection2(True)
        _select(model, name, "SKETCH")
        fm = model.FeatureManager
        attempts = []
        # FeatureCut4, 27 аргументов — ПРОВЕРЕНО ВЖИВУЮ на SW2024.
        # ВАЖНО: 2-й аргумент — это «Переставить сторону выреза» (flip side to cut,
        # режет ВСЁ СНАРУЖИ контура!) — всегда False, иначе вырез съест деталь.
        # Направление реза задаёт 3-й аргумент (пробуем оба).
        # 4-й аргумент = 1 (swEndCondThroughAll, насквозь); NormalCut=True.
        # Если вырез вернул None — контур не пересёк материал в этом направлении.
        for direction in (False, True):
            try:
                feat = fm.FeatureCut4(True, False, direction, 1, 0, 0.01, 0.01,
                                      False, False, False, False, 0.0, 0.0,
                                      False, False, False, False, True,
                                      True, True, True, True, False,
                                      0, 0.0, False, True)
                if feat is not None:
                    return _ok(feature=_get(feat, "Name"),
                               variant=f"FeatureCut4/27 dir={'reversed' if direction else 'default'}")
                attempts.append(f"FeatureCut4 dir={direction}: вернул None "
                                "(контур не попал в материал — проверь координаты эскиза)")
                model.ClearSelection2(True)
                _select(model, name, "SKETCH")
            except Exception as e:
                attempts.append(f"FeatureCut4 dir={direction}: {e}")
        raise RuntimeError(
            "Вырез не создан ни одним из известных вызовов:\n" + "\n".join(attempts) +
            "\nЗапиши макрос выреза в SolidWorks (Инструменты → Макрос → Записать), "
            "пришли его текст в чат — и вызови точную сигнатуру через execute_python."
        )
    return _run(job)


@mcp.tool()
def export_flat_pattern_dxf(output_path: str) -> str:
    """Экспорт РАЗВЁРТКИ активной листовой детали в DXF (для лазера/раскроя).
    output_path — полный путь к .dxf, папка должна существовать."""
    def job():
        model = _model(1)
        part = model  # IPartDoc получается из того же COM-объекта
        if not _get(model, "GetPathName"):
            raise RuntimeError("Деталь ещё не сохранена. Сначала save_document с указанием "
                               "save_as_path (.sldprt), иначе SolidWorks откроет модальный "
                               "диалог сохранения и вызов зависнет.")
        folder = os.path.dirname(output_path)
        if folder and not os.path.isdir(folder):
            raise RuntimeError(f"Папка не существует: {folder}")
        # 1 = swExportFlatPatternOption_RemoveBends (геометрия развёртки)
        ok = part.ExportFlatPatternView(output_path, 1)
        if not ok:
            raise RuntimeError("ExportFlatPatternView вернул False: убедись, что деталь листовая "
                               "(есть элемент SheetMetal) и путь доступен для записи.")
        return _ok(exported=output_path)
    return _run(job)


# ============================== СБОРКИ =====================================

@mcp.tool()
def get_components() -> str:
    """Состав активной сборки: имена компонентов, файлы, состояние."""
    def job():
        model = _model(2)
        comps = model.GetComponents(False)   # False = все уровни
        if comps is None:
            return _ok(components=[])
        out = []
        for c in comps:
            out.append({"name": _get(c, "Name2"),
                        "path": _get(c, "GetPathName"),
                        "suppressed": bool(_get(c, "IsSuppressed"))})
        return _ok(components=out,
                   note="Для сопряжений имя компонента в SelectByID2: 'ИмяДетали-1@ИмяСборки'")
    return _run(job)


@mcp.tool()
def add_component(path: str, x_mm: float = 0, y_mm: float = 0, z_mm: float = 0) -> str:
    """Вставить деталь/подсборку в активную сборку в точку (мм).
    path — полный путь к .sldprt/.sldasm."""
    def job():
        app = _app()
        model = _model(2)
        if not os.path.isfile(path):
            raise RuntimeError(f"Файл не найден: {path}")
        # компонент должен быть загружен в память — открываем тихо и невидимо
        app.DocumentVisible(False, 1 if path.lower().endswith(".sldprt") else 2)
        try:
            errs, warns = _byref_i4(), _byref_i4()
            cdoc = app.OpenDoc6(path, 1 if path.lower().endswith(".sldprt") else 2,
                                1, "", errs, warns)
            if cdoc is None:
                raise RuntimeError(f"Не удалось загрузить компонент (errors={errs.value}).")
        finally:
            app.DocumentVisible(True, 1)
            app.DocumentVisible(True, 2)
        comp = model.AddComponent5(path, 0, "", False, "", _mm(x_mm), _mm(y_mm), _mm(z_mm))
        if comp is None:
            raise RuntimeError("AddComponent5 вернул None — компонент не вставлен.")
        _get(model, "EditRebuild3")
        return _ok(component=_get(comp, "Name2"))
    return _run(job)


@mcp.tool()
def select_for_mate(entity1: str, type1: str, entity2: str, type2: str,
                    x1_mm: float = 0, y1_mm: float = 0, z1_mm: float = 0,
                    x2_mm: float = 0, y2_mm: float = 0, z2_mm: float = 0) -> str:
    """Выбрать две сущности для сопряжения (перед add_mate).
    Типы: 'FACE' (грань — укажи точку x,y,z в мм на грани в координатах СБОРКИ и entity=''),
    'PLANE' (плоскость по имени, напр. 'Спереди@Деталь1-1@Сборка1'),
    'EDGE', 'VERTEX'. Обе сущности получают метку выбора Mark=1."""
    def job():
        model = _model(2)
        model.ClearSelection2(True)
        ok1 = model.Extension.SelectByID2(entity1, type1, _mm(x1_mm), _mm(y1_mm), _mm(z1_mm),
                                          False, 1, _nothing(), 0)
        ok2 = model.Extension.SelectByID2(entity2, type2, _mm(x2_mm), _mm(y2_mm), _mm(z2_mm),
                                          True, 1, _nothing(), 0)
        if not (ok1 and ok2):
            raise RuntimeError(f"Выбор не удался: первая={bool(ok1)}, вторая={bool(ok2)}. "
                               f"Для FACE задай точку на грани; для PLANE — точное имя "
                               f"('Спереди@Деталь1-1@Сборка1').")
        return _ok(selected=2)
    return _run(job)


@mcp.tool()
def add_mate(mate_type: str = "coincident", alignment: str = "closest",
             distance_mm: float = 0.0, flip: bool = False) -> str:
    """Создать сопряжение из двух выбранных сущностей (сначала select_for_mate).
    mate_type: coincident | concentric | perpendicular | parallel | tangent | distance.
    alignment: aligned | antialigned | closest."""
    def job():
        model = _model(2)
        types = {"coincident": 0, "concentric": 1, "perpendicular": 2,
                 "parallel": 3, "tangent": 4, "distance": 5}
        aligns = {"aligned": 0, "antialigned": 1, "closest": 2}
        mt = types.get(mate_type.lower())
        if mt is None:
            raise RuntimeError(f"Неизвестный тип сопряжения: {mate_type}. Доступно: {list(types)}")
        al = aligns.get(alignment.lower(), 2)
        d = _mm(distance_mm)
        err = _byref_i4(-1)
        mate = model.AddMate5(mt, al, bool(flip), d, d, d, 0, 0, 0, 0, 0,
                              False, False, 0, err)
        if mate is None:
            raise RuntimeError(f"AddMate5 не создал сопряжение (код ошибки {err.value}). "
                               f"Частые причины: сущности не выбраны (select_for_mate), "
                               f"тип сопряжения не подходит к геометрии "
                               f"(например, concentric требует цилиндрических граней).")
        _get(model, "EditRebuild3")
        try:
            mate_name = _get(mate, "Name")
        except Exception:
            mate_name = str(mate)
        return _ok(mate=mate_name, error_code=err.value)
    return _run(job)


# ========================== АВАРИЙНЫЙ ЛЮК ==================================

@mcp.tool()
def execute_python(code: str) -> str:
    """Выполнить произвольный Python-код с доступом к SolidWorks COM API.
    Доступные переменные: swApp (ISldWorks), model (активный документ или None),
    win32com, pythoncom, VARIANT, MM (мм→м: MM(25.4)==0.0254).
    Результат: положи значение в переменную result и/или используй print().
    Применяй, когда готового инструмента нет или сигнатура API отличается."""
    def job():
        import win32com.client
        import pythoncom
        from win32com.client import VARIANT
        app = _app()
        env = {"swApp": app, "model": app.ActiveDoc,
               "win32com": win32com, "pythoncom": pythoncom, "VARIANT": VARIANT,
               "MM": _mm, "byref_i4": _byref_i4, "GET": _get, "NOTHING": _nothing()}
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            exec(code, env)
        return _ok(stdout=buf.getvalue(),
                   result=repr(env.get("result")) if "result" in env else None)
    return _run(job)


# ---------------------------------------------------------------------------
# Обёртка запуска в COM-потоке с единым форматом ошибок
# ---------------------------------------------------------------------------

def _run(job) -> str:
    # BaseException, а не Exception: даже MemoryError/KeyboardInterrupt не должны
    # выносить процесс — иначе клиент увидит "Server disconnected".
    try:
        return com_call(job)
    except BaseException as e:
        _log(f"ОШИБКА в {getattr(job, '__qualname__', 'job')}: {type(e).__name__}: {e}")
        return json.dumps({
            "status": "error",
            "error": str(e),
            "traceback": traceback.format_exc(limit=3),
            "hint": "Если это COM-ошибка сигнатуры метода — проверь help.solidworks.com "
                    "(API Help) для своей версии SolidWorks и вызови метод через execute_python.",
        }, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    # stdio-транспорт: Claude Desktop сам запускает этот процесс
    _log("=" * 60)
    _log(f"СТАРТ сервера v0.4, PID {os.getpid()}, python {sys.version.split()[0]}")
    try:
        mcp.run()
    except BaseException as e:
        _log(f"СЕРВЕР ОСТАНОВЛЕН: {type(e).__name__}: {e}\n" + traceback.format_exc())
        raise
    finally:
        _log("СТОП сервера")
