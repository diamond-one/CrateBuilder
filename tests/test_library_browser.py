import queue
import threading
import time
import tkinter as tk
import unittest

import customtkinter as ctk

from cratebuilder.library_ui import (LibraryBrowserMixin, library_item_actions,
                                     query_library_page, selected_library_items)


def item(index, **extra):
    return {"path": f"C:/library/pack/sound-{index}.wav", "name": f"Sound {index}",
            "provider": "Native Instruments", "pack": "Test pack", "source": "ni:test",
            "category": "Drums", "kind": "kick", "subtype": "acoustic", "bpm": 92,
            "key": "D minor", "size": 123456, "mtime_ns": index,
            "favourite": False, "hidden": False, "used_count": 0, **extra}


class MetadataStore:
    def __init__(self, rows, log=None, fail_path=None):
        self.rows = rows
        self.log = log if log is not None else []
        self.fail_path = fail_path
        self.owner = threading.get_ident()

    def _check(self):
        if threading.get_ident() != self.owner:
            raise AssertionError("SQLite store crossed its owning worker thread")

    def _matches(self, filters):
        self._check()
        return [row for row in self.rows if
                (not filters.get("search") or filters["search"].casefold() in row["name"].casefold())
                and (not filters.get("source") or filters["source"] == row["source"])
                and (not filters.get("category") or filters["category"] == row["category"])
                and (not filters.get("favourites_only") or row["favourite"])
                and bool(row["hidden"]) == bool(filters.get("hidden_only"))
                and (not filters.get("unused_only") or row["used_count"] == 0)]

    def count_items(self, **filters):
        self.log.append(("count", filters))
        return len(self._matches(filters))

    def query_items(self, limit=200, offset=0, **filters):
        self.log.append(("query", {**filters, "limit": limit, "offset": offset}))
        return [dict(row) for row in self._matches(filters)[offset:offset + limit]]

    def list_sources(self):
        self._check()
        return [{"id": "ni:test", "name": "Test pack", "provider": "Native Instruments", "count": len(self.rows)}]

    def set_mark(self, path, **changes):
        self._check()
        if path == self.fail_path:
            raise OSError("Metadata database temporarily unavailable")
        self.log.append(("mark", {"path": path, **changes}))
        next(row for row in self.rows if row["path"] == path).update(changes)


class Variable:
    def __init__(self, value):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class Widget:
    def __init__(self):
        self.owner = threading.get_ident()
        self.options = {}

    def configure(self, **options):
        if threading.get_ident() != self.owner:
            raise AssertionError("A query worker touched a UI widget")
        self.options.update(options)


class Listing(Widget):
    def __init__(self):
        super().__init__()
        self.rows, self.selection = [], ()

    def curselection(self):
        return self.selection

    def delete(self, *_):
        self.rows.clear()
        self.selection = ()

    def insert(self, _, value):
        self.rows.append(value)


class Browser(LibraryBrowserMixin):
    def __init__(self, rows):
        self.rows, self.log, self.scheduled = rows, [], {}
        self.busy, self._closed = False, False
        self.events = queue.Queue()
        self._library_query_lock = threading.Lock()
        self.marks_changed = 0
        self.previews, self.used, self.exports = [], [], []
        self.fail_path = None
        state = {"active": True, "token": object(), "serial": 1, "offset": 0, "after": None,
                 "items": [], "total": 0, "source_id": None, "source_map": {},
                 "updating_filters": False, "marking": False,
                 "search": Variable(""), "source": Variable("All packs"), "category": Variable("All"),
                 "favourites": Variable(False), "hidden": Variable(False), "unused": Variable(False),
                 "listing": Listing()}
        for key in ("status", "source_menu", "detail_title", "detail_metadata", "detail_path", "detail_reason",
                    "preview_button", "use_button", "favourite_button", "hide_button", "export_button",
                    "previous_button", "next_button", "page_label"):
            state[key] = Widget()
        self._library_browser = state

    def _library_worker_store(self):
        return MetadataStore(self.rows, self.log, self.fail_path)

    def after(self, delay, callback):
        token = object()
        self.scheduled[token] = (delay, callback)
        return token

    def after_cancel(self, token):
        self.scheduled.pop(token, None)

    def run_scheduled(self):
        tasks, self.scheduled = list(self.scheduled.values()), {}
        for _, callback in tasks:
            callback()

    def dispatch(self):
        event, payload = self.events.get(timeout=3)
        self.handle_library_event(event, payload)
        return event, payload

    def library_marks_changed(self):
        self.marks_changed += 1

    def preview_library_item(self, value):
        self.previews.append(value)

    def use_library_item(self, value):
        self.used.append(value)

    def export_library_items(self, values):
        self.exports.append(values)


class LibraryBrowserTests(unittest.TestCase):
    def test_offscreen_dialog_search_and_explicit_multi_export(self):
        class Gui(LibraryBrowserMixin, ctk.CTk):
            def __init__(self):
                super().__init__()
                self.geometry("900x650+10000+10000")
                self.busy, self._closed = False, False
                self.events = queue.Queue()
                self.rows = [item(index, category="Loops" if index == 0 else "Drums") for index in range(401)]
                self.exports = []

            def label(self, parent, text, size=13, color="#F0F2ED", bold=False, **kwargs):
                return ctk.CTkLabel(parent, text=text, text_color=color,
                    font=ctk.CTkFont(family="Segoe UI", size=size, weight="bold" if bold else "normal"), **kwargs)

            def button(self, parent, text, command, primary=False, **kwargs):
                return ctk.CTkButton(parent, text=text, command=command, **kwargs)

            def _library_worker_store(self):
                return MetadataStore(self.rows)

            def import_ni_expansions(self):
                raise AssertionError("Import must require a user action")

            def export_library_items(self, rows):
                self.exports.append(rows)

        try:
            host = Gui()
        except tk.TclError as exc:
            self.skipTest(f"Desktop GUI unavailable: {exc}")
        try:
            host.browse_library()
            host.library_window.geometry("900x650+10000+10000")

            def wait_for(predicate):
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    host.update()
                    while not host.events.empty():
                        event, payload = host.events.get_nowait()
                        host.handle_library_event(event, payload)
                    if predicate():
                        return
                    threading.Event().wait(.01)
                self.fail("Library dialog did not reach the expected state")

            wait_for(lambda: host._library_browser["total"] == 401)
            state = host._library_browser
            self.assertEqual(state["listing"].size(), 200)
            state["listing"].selection_set(0, 1)
            host._library_export()
            self.assertEqual([row["name"] for row in host.exports[0]], ["Sound 0", "Sound 1"])
            state["listing"].selection_clear(0, "end")
            state["listing"].selection_set(0)
            host.sync_library_controls()
            host.update_idletasks()
            self.assertEqual(state["use_button"].cget("state"), "disabled")
            for key in ("preview_button", "use_button", "favourite_button", "hide_button", "export_button"):
                button = state[key]
                self.assertLessEqual(button.winfo_rooty() - host.library_window.winfo_rooty() + button.winfo_height(),
                                     host.library_window.winfo_height())
                self.assertLessEqual(button.winfo_rootx() - host.library_window.winfo_rootx() + button.winfo_width(),
                                     host.library_window.winfo_width())
            state["search"].set("Sound 399")
            wait_for(lambda: state["total"] == 1)
            self.assertEqual(state["listing"].size(), 1)
            self.assertEqual(state["items"][0]["name"], "Sound 399")
            self.assertLessEqual(state["status"].winfo_y() + state["status"].winfo_height(),
                                 host.library_window.winfo_height())
            host.close_library_browser()
            self.assertIsNone(host._library_browser)
        finally:
            host._closed = True
            host.destroy()

    def test_large_catalogue_fetches_and_renders_only_one_200_row_page(self):
        rows = [item(index) for index in range(44001)]
        browser = Browser(rows)
        browser._start_library_query(browser._library_browser, 1)
        browser.dispatch()
        state = browser._library_browser
        self.assertEqual(len(state["items"]), 200)
        self.assertEqual(len(state["listing"].rows), 200)
        self.assertEqual(state["total"], 44001)
        request = next(details for method, details in browser.log if method == "query")
        self.assertEqual((request["limit"], request["offset"]), (200, 0))
        self.assertEqual(state["items"][17]["key"], "D minor")
        self.assertEqual(state["items"][17]["mtime_ns"], 17)
        self.assertIn("44,001", state["page_label"].options["text"])

    def test_final_page_is_clamped_after_filter_or_mark_removes_rows(self):
        rows = [item(index) for index in range(205)]
        result = query_library_page(MetadataStore(rows), {}, offset=800)
        self.assertEqual((result["offset"], len(result["items"])), (200, 5))
        rows[:] = rows[:150]
        result = query_library_page(MetadataStore(rows), {}, offset=200)
        self.assertEqual((result["offset"], len(result["items"])), (0, 150))

    def test_source_category_and_metadata_filters_are_forwarded_precisely(self):
        rows = [item(1, category="One Shots", favourite=True),
                item(2, category="Loops", favourite=True), item(3, category="One Shots")]
        filters = {"search": "Sound", "source": "ni:test", "category": "One Shots",
                   "favourites_only": True, "hidden_only": False, "unused_only": True}
        log = []
        result = query_library_page(MetadataStore(rows, log), filters)
        self.assertEqual([row["name"] for row in result["items"]], ["Sound 1"])
        self.assertEqual(log[0], ("count", filters))
        self.assertEqual(log[1], ("query", {**filters, "limit": 200, "offset": 0}))

    def test_loops_instruments_and_rex2_remain_explorable_but_not_pad_assignable(self):
        self.assertTrue(library_item_actions(item(1))["use"])
        self.assertTrue(library_item_actions(item(1, category="One Shots", kind="unknown"))["use"])
        self.assertEqual(library_item_actions(item(2, category="Loops"))["preview"], True)
        self.assertFalse(library_item_actions(item(2, category="Loops"))["use"])
        self.assertFalse(library_item_actions(item(3, category="Instruments"))["use"])
        rex = library_item_actions(item(4, path="C:/pack/break.rx2", category="Loops"))
        self.assertFalse(rex["preview"])
        self.assertFalse(rex["use"])
        self.assertIn("REX2", rex["reason"])

    def test_export_snapshot_contains_only_explicitly_selected_visible_rows(self):
        rows = [item(index) for index in range(6)]
        result = selected_library_items(rows, [4, 1, 4, -1, 6, "2", True])
        self.assertEqual([row["name"] for row in result], ["Sound 1", "Sound 4"])
        result[0]["name"] = "Changed snapshot"
        self.assertEqual(rows[1]["name"], "Sound 1")

    def test_debounce_invalidates_old_queries_without_global_busy_or_pad_changes(self):
        browser = Browser([item(1), item(2)])
        browser.busy = True
        state = browser._library_browser
        browser._schedule_library_query(reset_page=True)
        old_callback = next(iter(browser.scheduled.values()))[1]
        state["search"].set("Sound 2")
        browser._schedule_library_query(reset_page=True)
        self.assertEqual(len(browser.scheduled), 1)
        self.assertEqual(next(iter(browser.scheduled.values()))[0], 250)
        old_callback()
        self.assertFalse(browser.log)
        browser.run_scheduled()
        browser.dispatch()
        self.assertTrue(browser.busy)
        self.assertEqual([row["name"] for row in state["items"]], ["Sound 2"])

    def test_late_result_after_browser_close_is_ignored(self):
        browser = Browser([item(1)])
        state = browser._library_browser
        payload = {"token": state["token"], "serial": 1,
                   "result": {"items": [item(1)], "total": 1, "offset": 0, "sources": []}}
        browser._library_destroyed(state)
        self.assertTrue(browser.handle_library_event("library_results", payload))
        self.assertFalse(state["listing"].rows)
        self.assertIsNone(browser._library_browser)

    def test_marking_uses_only_selected_paths_and_refreshes_root_even_after_close(self):
        browser = Browser([item(index) for index in range(5)])
        state = browser._library_browser
        state["items"] = [dict(row) for row in browser.rows]
        state["listing"].selection = (1, 3)
        browser._library_mark("hidden")
        browser._library_destroyed(state)
        browser.dispatch()
        changed = [row["name"] for row in browser.rows if row["hidden"]]
        self.assertEqual(changed, ["Sound 1", "Sound 3"])
        self.assertEqual(browser.marks_changed, 1)
        self.assertEqual(len([entry for entry in browser.log if entry[0] == "mark"]), 2)

    def test_partial_mark_failure_reports_saved_count_and_refreshes_applied_marks(self):
        browser = Browser([item(1), item(2)])
        browser.fail_path = browser.rows[1]["path"]
        state = browser._library_browser
        state["items"] = [dict(row) for row in browser.rows]
        state["listing"].selection = (0, 1)
        browser._library_mark("favourite")
        browser.dispatch()
        self.assertTrue(browser.rows[0]["favourite"])
        self.assertFalse(browser.rows[1]["favourite"])
        self.assertEqual(browser.marks_changed, 1)
        self.assertIn("Saved 1 of 2", state["status"].options["text"])
        self.assertFalse(state["marking"])

    def test_loop_use_is_blocked_and_multi_export_passes_exact_selection(self):
        browser = Browser([item(1, category="Loops"), item(2, category="One Shots")])
        state = browser._library_browser
        state["items"] = browser.rows
        state["listing"].selection = (0,)
        browser._library_use()
        browser._library_preview()
        self.assertFalse(browser.used)
        self.assertEqual(browser.previews[0]["category"], "Loops")
        state["listing"].selection = (1,)
        browser._library_use()
        self.assertEqual(browser.used[0]["category"], "One Shots")
        state["listing"].selection = (0, 1)
        browser._library_use()
        browser._library_export()
        self.assertEqual(len(browser.used), 1)
        self.assertEqual([row["name"] for row in browser.exports[0]], ["Sound 1", "Sound 2"])


if __name__ == "__main__":
    unittest.main()
