# План очистки репозитория `experemental/`

Версия отчёта: подтверждающий аудит после первичного grep-сканирования.
Все находки подтверждены повторным анализом по всей кодовой базе.

> **Правило:** изменения НЕ применены. Это только план. Все диапазоны строк указаны для текущего состояния `wiwang_poster_loop.py` (14 094 строки) и других файлов.

---

## Раздел 1. УДАЛИТЬ

### 1.1. Мёртвые ключи конфига (20 шт.)

Все 20 ключей из первичного списка подтверждены — они присутствуют только в блоке дефолтов `wiwang_poster_loop.py` (~строки 900–1200) и в `sale/config.json`, **но нигде не читаются** через `cfg.get(...)` или `cfg["..."]`.

Подтверждено по полнотекстовому grep'у — каждый ключ имеет ровно 2 вхождения: дефолт + persisted JSON.

| Ключ | Строка в `wiwang_poster_loop.py` |
|---|---|
| `vehicle_click_confirm_enabled` | 921 |
| `vehicle_click_confirm_skip_if_form_ready` | 922 |
| `vehicle_click_confirm_check_retries` | 923 |
| `vehicle_click_confirm_check_delay` | 924 |
| `vehicle_click_confirm_disabled_stems` | 925 |
| `post_ok_wait_vehicle_list` | 937 |
| `file_dialog_clear_before_paste` | 1015 |
| `file_dialog_paste_force_ctrl_v` | ~ |
| `file_dialog_paste_use_ctrl_l` | ~ |
| `file_dialog_path_focus_delay` | ~ |
| `file_dialog_type_char_delay` | ~ |
| `file_dialog_use_f4_focus` | ~ |
| `start_nudge_click` | 1062 |
| `type_fallback_clipboard_punct` | ~ |
| `fast_scan_edge_enabled` | ~ |
| `fast_scan_edge_aperture` | 1101 |
| `fast_scan_edge_l2` | ~ |
| `fast_scan_edge_low` | ~ |
| `fast_scan_edge_high` | ~ |
| `plate_read_min_confidence` | 1140 |

**Что удалить:**
- Удалить эти строки из блока дефолтов в `wiwang_poster_loop.py` (~900–1200).
- Удалить эти ключи из `sale/config.json` (можно автоматически через `config_compat.save` после удаления из дефолтов — но безопаснее удалить вручную).

**Безопасность:** низкий риск. Если кто-то «случайно» вернёт код, который их читает — `cfg.get("key", default)` всегда работает с дефолтом-аргументом.

### 1.2. Мёртвые функции в `wiwang_poster_loop.py`

Подтверждены как unreachable (ровно 1 вхождение в файле = объявление, нет вызовов):

| Функция | Строка | Что делает | Удалить? |
|---|---|---|---|
| `_tmpl_gray_cached` | 3652 | Кеширующая загрузка серого темплейта — заменена локальной логикой | **ДА** |
| `is_form_ready_quick` | 3804 | Альт-проверка готовности формы | **ДА** |
| `_win_get_focus_hwnd` | 4144 | win32 helper | **ДА** |
| `_win_get_text` | 4163 | win32 helper | **ДА** |
| `_win_set_text` | 4176 | win32 helper | **ДА** |
| `_load_vehicle_settings` | 4998 | Чтение настроек машины | **ДА** |
| `_plate_ref_path` | 5007 | Путь к референс-плейту | **ДА** |
| `choose_price_with_schedule` | 1529 | Старый выбор цены по расписанию (заменён pricing-AI) | **ДА** |
| `_format_pop_delta` | 11382 | Форматирование «дельты популярности» | **ДА** |
| `_no_op_factory` | 13136 | Фабрика заглушек — есть параллельная `_v6_missing_cb_factory` | **ДА** |
| `row_float_list` | 8586 | Локальная функция-хелпер внутри UI-метода, не вызывается | **ДА** (можно оставить — это внутренний хелпер) |

**Не удалять (это публичный API / debug hook):**
| Функция | Строка | Причина |
|---|---|---|
| `emit` | 700 | Метод класса `_LogBridgeHandler(logging.Handler)` — вызывается фреймворком logging. **НЕ удалять.** |
| `enter_create_rent_force` | 5916 | Debug-обёртка для принудительного входа в Create Rent. Не вызывается, но это явный public helper. **Рекомендую оставить.** |

### 1.3. Мёртвые функции в gui-модулях

Подтверждено: ни одна не используется во всём проекте (включая `wiwang_poster_loop.py`).

| Функция | Файл:строка | Удалить? |
|---|---|---|
| `AppState.set_metrics` | `gui/state/app_state.py:39` | **ДА**, но это публичный setter — рассмотрите оставить (внешний код через AppState ничего не пишет, но API есть) |
| `AppState.set_ui_prefs` | `gui/state/app_state.py:47` | **ДА**, аналогично |
| `AppState.add_error` | `gui/state/app_state.py:51` | **ДА**, аналогично |
| `AdminGate.ensure_config` | `gui/auth/admin_gate.py:18` | **ОСТАВИТЬ** — bootstrapping admin password из ENV. Это нужный init, который, вероятно, должен вызываться при загрузке cfg. **Рекомендую починить вызов**, см. раздел 3. |
| `AdminGate.lockout_remaining` | `gui/auth/admin_gate.py:72` | **ОСТАВИТЬ** — должен показываться юзеру при попытке разблокировки. См. раздел 3. |
| `AdminGate.accept_override` | `gui/auth/admin_gate.py:78` | **ОСТАВИТЬ** — emergency-override через `ADMIN_PASSWORD` ENV. Логически нужен. См. раздел 3. |
| `get_pref` | `gui/persistence/ui_prefs.py:17` | **ДА** (helper-обёртка не используется) |
| `set_pref` | `gui/persistence/ui_prefs.py:23` | **ДА** (helper-обёртка не используется) |
| `TgDashboard.clear_search` | `gui/views/tg_dashboard.py:149` | **ДА** |
| `TgDashboard.set_status` | `gui/views/tg_dashboard.py:152` | **ДА** |
| `ItemSaleTab.append_log` | `gui/views/item_sale_tab.py:256` | **ОСТАВИТЬ** — публичный метод для UI логирования. Скорее всего недокол-бэк (см. раздел 3) |
| `backup_config` | `config_compat.py:227` | **ОСТАВИТЬ** (public API) |
| `load_config_safe` | `config_compat.py:217` | **ОСТАВИТЬ** (public alias) |
| `LoopManager.stop_and_join` | `loop_manager.py:96` | **ОСТАВИТЬ** (public helper) |
| `get_best_qty_from_table` | `item_actions.py:1018` | **ОСТАВИТЬ** — публичный helper, может быть подключён позже |
| `read_available_qty` | `item_actions.py:997` | **ОСТАВИТЬ** — публичный helper |

### 1.4. Unused-импорты и неиспользуемые локальные

Все проверены `pyflakes`. Безопасно убрать:

| Файл:строка | Что | Действие |
|---|---|---|
| `gui/components/status_bar.py:9` | `accent = ...` | удалить (`accent` нигде не используется) |
| `gui/components/status_bar.py:10` | `border = ...` | удалить |
| `gui/components/status_bar.py:12` | `fg = ...` | удалить |
| `gui/components/tables.py:1` | `import tkinter as tk` | удалить — используется только `ttk` |
| `gui/views/admin_panel.py:24` | `_muted = ...` | удалить |
| `gui/views/admin_panel.py:27` | `_danger = ...` | удалить |
| `gui/views/admin_panel.py:28` | `_success = ...` | удалить |
| `gui/app_gui.py:3` | `from typing import ... Optional ...` | убрать `Optional` |
| `gui/app_gui.py:6` | `from tkinter import ..., messagebox` | убрать `messagebox` |
| `gui/app_gui.py:134` | `_accent2 = ...` | удалить (не используется) |
| `gui/app_gui.py:254` | `_btn = ...` | удалить или использовать |
| `loop_manager.py:8` | `from typing import ..., Tuple` | убрать `Tuple` |
| `item_actions.py:310` | `from pynput.keyboard import ..., Key as _Key` | убрать `Key as _Key` |
| `item_sale_monitor.py:60` | `from dataclasses import ..., field` | убрать `field` |
| `item_sale_monitor.py:61` | `from typing import ..., Tuple` | убрать `Tuple` |
| `cleaner.py:39` | `import os` | убрать |
| `cleaner.py:40` | `import glob` | убрать |

### 1.5. TODO/FIXME

Полный grep по `TODO|FIXME|XXX|HACK` в `experemental/` дал **0 совпадений**. Очищать нечего.

### 1.6. Закомментированные блоки кода

Поиск блоков ≥ 8 подряд идущих строк-комментариев, выглядящих как закомментированный код, дал **0 совпадений**. Очищать нечего.

---

## Раздел 2. ДУБЛИКАТЫ — какую версию оставлять

### 2.1. `_app_clear_logs` — три определения

**Внимание: первичный аудит ошибочно сказал «вторая версия побеждает» — это НЕ так.**

| № | Строка | Имя | Биндинг |
|---|---|---|---|
| v1 | 13207 | `_app_clear_logs` (краткая) | `setattr(App, "clear_logs", _app_clear_logs)` в `_bind_stability_methods()` на строке 13265 — выполняется при импорте |
| v2 | 13753 | `_app_clear_logs` (расширенная) | `setattr` на 13790 — но строки 13752+ выполняются ТОЛЬКО после возврата из `main()` (см. раздел 3.1) |
| stub | 14037 | `_app_clear_logs_stub` | биндинг в `_install_stability_bootstrap()` — тоже после `main()` |

**Что побеждает в реальности:** v1 (13207), потому что только она реально успевает забиндиться до запуска `App()`. Все остальные `setattr` на класс App проходят, но только после того, как mainloop завершится (т.е. на shutdown). При этом v2 и stub используют `if not hasattr(App, "clear_logs")` — они всё равно ничего не делают, т.к. v1 уже там.

**Что оставить, что удалить:**
- Оставить **v1 (13207)** + биндинг в `_bind_stability_methods` (13207–13266).
- **Удалить** v2 (13753–13785) и её биндинг (13787–13792).
- **Удалить** stub (14037–14063) и его биндинг (часть `_install_stability_bootstrap`).

> Примечание: см. раздел 4 — этот «трёхслойный bootstrap» сам по себе сомнителен; есть смысл сначала разобраться с архитектурой, а потом резать.

### 2.2. `_set_editor_typing`, `_bump_editor_typing`, `_mark_editor_dirty`

Тоже три места:
- **Closures в `App.__init__`** (7112–7133) — присваиваются как `self._set_editor_typing = <closure>` (instance attribute)
- **Module-level defs** (13664–13682) — биндятся в класс App в блоке 13684–13708

**Что побеждает:** instance attribute (closure) перекрывает class method. Когда binding из 12147 делает `self._set_editor_typing(True)`, питон находит сначала instance attr.

**Дополнение:** module-level версии устанавливают атрибут `_editor_last_typing_ts`, но он **нигде не читается** (grep подтвердил). Так что функционально ничего не теряется.

**Что оставить:**
- Оставить **closures в `__init__`** (7112–7133) — они работают.
- **Удалить** module-level defs (13664–13682) и записи `'_set_editor_typing', '_bump_editor_typing', '_mark_editor_dirty'` из списка bind_names (строки 13702–13704).

### 2.3. Псевдо-дубли (НЕ являются дублями, не трогать!)

Эти выглядят как дубли по grep'у, но это разные scope'ы / классы:

- `load` / `save` в `ConfigManager` (1284, 1310) **vs** `Stats` (1767, 1787) — разные классы.
- `_cancel` в `Tooltip` (1572) **vs** `RegionDrawer` (1743) — разные классы.
- `_is_bad_capture`, `_build_from_gray`, `_build_from_shot` в `_fast_scan_build_legacy` (2629/2637/2732) **vs** `fast_scan_build` (2896/2908/3065) — это **legacy** и **новый** движки fast-scan. `_fast_scan_build_legacy` вызывается из `fast_scan_build` на строке 2841 при `fast_scan_use_legacy_engine=True`. Оба нужны. **Не трогать.**

---

## Раздел 3. ПОЧИНИТЬ

### 3.1. Критично: пост-`__main__` блок недостижим в продакшне

**Локация:** `wiwang_poster_loop.py:13736–14094` (≈ 358 строк).

```python
13736  if __name__ == "__main__":
13737      try:
13738          main()         # ← блокирует на app.mainloop()
...
13752  # --- FIX: ensure App has clear_logs ---
13753  def _app_clear_logs(self): ...
...
14093  _install_stability_bootstrap()
14094  # ===================== /STABILITY BOOTSTRAP =====================
```

**Проблема:** код после `if __name__ == "__main__":` блока (строки 13752+) выполняется при импорте модуля. **Но в продакшне модуль запускается как `__main__`, попадает в `if`, вызывает `main()`, который блокируется на `app.mainloop()`**. Возврат из `main()` происходит только после закрытия окна — т.е. вся «v2 стабильность», `_install_stability_bootstrap()` и ~350 строк fallback-bind-методов **запускаются на shutdown и эффекта не имеют**.

В loop_manager.py при `from wiwang_poster_loop import ...` модуль уже в `sys.modules` — повторно не выполняется.

**Что починить:**
- **Вариант A (безопасный):** перенести блоки 13752–14092 ВЫШЕ строки 13736 (`if __name__ == "__main__":`), чтобы они отработали до запуска mainloop. Тогда они станут реально активны как safety net.
- **Вариант B (агрессивный, требует подтверждения юзера):** если v1 уже покрывает clear_logs (а она покрывает — см. 2.1), просто удалить блок 13752–14092 целиком (вместе с v2 и stub).

**Риск:** если эти fallback-методы НИКОГДА не нужны на практике — текущее «работает». Если когда-то нужно — оно сломано прямо сейчас, но никто не заметил.

**Рекомендация:** при варианте A разобраться, нужны ли реально 4 разные `setattr(App, "clear_logs", ...)`. Сейчас стек выглядит как накопление защитных слоёв против merge-проблем, которые уже исправлены.

### 3.2. `AdminGate.ensure_config` не вызывается — admin-gate инициализация неполная

**Локация:** `gui/auth/admin_gate.py:18`, `gui/app_gui.py:57, 592`.

**Что есть:** `AppGUI` создаёт `self.admin_gate = AdminGate()`, потом на строке 592 вызывает `self.admin_gate.check_password(cfg, candidate)`.

**Чего нет:** нигде не вызывается `admin_gate.ensure_config(cfg)`, который должен:
- настроить salt
- захешировать дефолтный пароль из `ENV[ADMIN_PASSWORD]`
- установить `enabled` из `ADMIN_GATE_ENABLED=1`

**Что починить:** в `AppGUI.__init__` (или при первом обращении к admin-gate) вызвать `cfg, changed = self.admin_gate.ensure_config(cfg)` и сохранить cfg если `changed`. Иначе админ-режим в свежей установке не инициализируется и не сможет принять `ADMIN_PASSWORD` из окружения.

### 3.3. `AdminGate.lockout_remaining` не показывается юзеру

**Локация:** `gui/auth/admin_gate.py:72`, `gui/app_gui.py:~590` (где обрабатывается ввод пароля).

**Что починить:** при отказе `check_password` дополнительно вызвать `remaining = admin_gate.lockout_remaining()` и если не None — показать «Заблокировано на {remaining}s». Сейчас юзер получает только «Неверный пароль» без объяснения, что аккаунт залочен на 60с.

### 3.4. `AdminGate.accept_override` (emergency password) не подключён

**Локация:** `gui/auth/admin_gate.py:78`.

**Что есть:** механизм восстановления через `ENV[ADMIN_PASSWORD]` (если юзер задал пароль в окружении, он принимается и переустанавливает хеш в cfg).

**Чего нет:** вызова на UI-стороне.

**Что починить:** в `AppGUI` (~592) если `check_password` упал — попробовать `accept_override(cfg, candidate)` и если True — сохранить cfg, разблокировать. Это безопасный аварийный механизм при потере пароля.

### 3.5. `ItemSaleTab.append_log` не вызывается

**Локация:** `gui/views/item_sale_tab.py:256`.

**Контекст:** UI-метод для логирования в панель item-sale-tab, но ни один внешний код его не вызывает.

**Что починить:** проверить, не отвалился ли callback при репорте `item_sale_monitor` событий. Если в самой UI таблице события всё-таки отображаются (через `_refresh_stats_panel`), то `append_log` действительно не нужен. Если же лог-панель пустая — это баг (потеряли подписку).

**Рекомендация:** уточнить у юзера, нужна ли лог-панель в item-sale tab. Если нужна — найти куда подключить вызов `append_log(...)` из мониторинга. Если не нужна — можно удалить как метод **И** убрать сам `_log_text` widget из `_build_ui`.

### 3.6. `TgDashboard.set_status` не вызывается

**Локация:** `gui/views/tg_dashboard.py:152`.

**Контекст:** `StatusBar` создаётся внутри dashboard'а, метод `set_status` обновляет текст. Внешний код ни разу не вызывает `self._tg_dashboard.set_status(...)`.

**Что починить или удалить:** в `gui/app_gui.py` в цикле обновления (где есть `self._tg_dashboard.update_logs/pulse/...`) добавить `self._tg_dashboard.set_status(state_text)` с актуальным статусом трекера. Либо удалить метод и `StatusBar` из dashboard, если он реально не нужен.

### 3.7. `TgDashboard.clear_search` не вызывается

**Локация:** `gui/views/tg_dashboard.py:149`.

**Контекст:** очистка поля поиска. Скорее всего должна вызываться по событию «закрыли модалку поиска» или «нажали Esc».

**Рекомендация:** проверить, есть ли вообще такое UX-требование. Если нет — удалить. Если есть — добавить биндинг к Esc.

### 3.8. f-строки без подстановок

`item_actions.py:636, 659` — `_log(cfg, f"[actions] Кнопка корзины найдена по шаблону")` и аналогичные. Префикс `f` лишний (внутри только литерал). Косметика, можно убрать.

### 3.9. `field_retries` / `paste_retries` хранятся как float

`sale/config.json:52,56` — значения `2.0`. Используются как int (число попыток). Не баг, но рекомендую сохранить как `2` для чистоты.

### 3.10. `_app_clear_logs` v2 (13753) выглядит как «улучшенная» версия v1

v2 содержит дополнительные имена виджетов (`txt_log_view`, `txt_logs_view`, `log_view`), которых нет в v1. Поскольку v2 в текущей сборке мёртвая (см. 3.1), фактически работает v1, а v2 — идея на будущее, которая не активирована.

**Что починить:** ЛИБО мерджить лучшие имена-кандидаты в v1 (13207–13232) и удалить v2/stub, ЛИБО оставить только v2 и переместить её ВЫШЕ `if __name__`, удалив v1. Рекомендую первый вариант — меньше путаницы.

---

## Раздел 4. РИСКОВАННОЕ — НЕ ТРОГАТЬ без подтверждения

### 4.1. Трёхуровневая система биндингов после `__main__`

Блок 13752–14094 — это, по-видимому, исторический «защитный слой» из времён мерджей saleNEW → experemental, когда часть `App`-методов оставалась orphan'ами на module-level. Сейчас он мёртв (см. 3.1), но удаление **может вскрыть скрытые ошибки**, если найдётся хоть один `App.<method>`, который сейчас держится только за счёт `_bind_orphan_app_methods()` (строка 13712).

Прежде чем резать:
1. Сделать `python -c "import wiwang_poster_loop; from wiwang_poster_loop import App; print([n for n in dir(App) if not n.startswith('__')])"` — увидеть полный список методов.
2. Затем убрать кусок 13752–14094, повторить, сравнить — должны быть **те же** методы.
3. Только потом удалять.

### 4.2. `enter_create_rent_force` (5916)

Не вызывается, но это **debug helper** для bypass cooldown'а. При проблемах с автоматизацией такой helper нужен — рекомендую оставить.

### 4.3. `_load_vehicle_settings` / `_plate_ref_path` (4998, 5007)

Выглядят как часть незавершённой фичи (per-vehicle settings). Сейчас не подключены. **Удалить можно**, но если планируется их подключить — оставить.

### 4.4. `read_available_qty` / `get_best_qty_from_table` (item_actions.py)

Публичные функции в `item_actions` — модуль предположительно используется как library API из других модулей (`item_sale_monitor`?). Грепом не нашёл вызовов, но это не означает «никогда не нужны» — оставить.

### 4.5. Удаление 20 ключей конфига

Сам код безопасен, но **после удаления из дефолтов** старый `sale/config.json` будет содержать «лишние» ключи, которые `ConfigManager.save` сохранит обратно (если он сохраняет всё подряд). Проверьте, чтобы persisted JSON был очищен — иначе пользователи будут продолжать таскать мёртвые поля.

### 4.6. `AppState.set_metrics` / `set_ui_prefs` / `add_error`

Эти setter'ы выглядят как часть **запланированной но не реализованной** интеграции AppState ↔ метрики/ошибки. Если планируется в ближайшее время прикрутить — оставить. Если нет — можно удалять, но честнее **починить интеграцию**, чем удалять API.

### 4.7. Editor-typing module-level дубли (13664–13682)

Удаление этих module-level функций безопасно (см. 2.2). НО: если кто-то снаружи делает `App._set_editor_typing(instance, True)` через **class lookup** (минуя instance attr), это сломается. Грепом такого вызова не нашёл, риск низкий, но не нулевой.

---

## Сводка по приоритетам

| Приоритет | Что делать | Risk |
|---|---|---|
| 🟢 Низкий риск | Удалить 20 dead-keys конфига + одноимённые ключи из `sale/config.json` | safe |
| 🟢 Низкий риск | Удалить 11 dead-функций (раздел 1.2, кроме `emit` и `enter_create_rent_force`) | safe |
| 🟢 Низкий риск | Убрать unused-импорты и неиспользуемые локальные (раздел 1.4) | safe |
| 🟡 Средний | Починить admin-gate инициализацию (3.2–3.4) | требует тестирования |
| 🟡 Средний | Решить судьбу `append_log`, `set_status`, `clear_search` в gui-views (3.5–3.7) — починить или удалить с виджетами | требует UX-решения |
| 🟡 Средний | Удалить module-level editor-typing дубли (2.2) | low, проверить отсутствие external usage |
| 🟡 Средний | Удалить v2/stub `_app_clear_logs` (раздел 2.1) | low, если фикс 3.1 принят |
| 🔴 Высокий | Перенести/удалить блок 13752–14094 после `__main__` (3.1) | требует обязательной проверки 4.1 |
| 🔴 Высокий | Удалить `_load_vehicle_settings`, `_plate_ref_path` (4.3) | подтвердить с юзером — не часть незаконченной фичи? |

**Итого:** безопасно убрать ~700–900 строк кода и 20 dead-keys + починить 6 явных интеграционных багов (admin-gate, dashboard status, item-sale log). Архитектурная санация bootstrap-блока — отдельная задача, требующая дополнительной проверки.
