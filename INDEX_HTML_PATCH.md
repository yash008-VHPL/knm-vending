# index.html — surgical patches for the new tabs

Two small edits. Do these by hand (or via `git apply` if you prefer) at cutover. The 1,731-line file is too risky to fully rewrite.

## Patch 1 — replace single "Work Orders" tab button with three new buttons

**Find** (around line 365):

```html
    <button class="tab-btn"        onclick="switchTab('workorders', this); if(window.__woInit) window.__woInit();">Work Orders</button>
```

**Replace with:**

```html
    <button class="tab-btn"        onclick="switchTab('fault_report', this); if(window.__frInit) window.__frInit();">Fault Report</button>
    <button class="tab-btn"        onclick="switchTab('tech_support', this); if(window.__tsInit) window.__tsInit();">Tech Support</button>
    {% if role == 'admin' or role == 'field_manager' %}
    <button class="tab-btn"        onclick="switchTab('kb_admin', this); if(window.__kbInit) window.__kbInit();">Manage KB</button>
    {% endif %}
```

## Patch 2 — replace the workorders include with the three new tab includes

**Find** (around line 716):

```html
    <!-- ══════════════════════════════════════════
         TAB: Work Orders (added — see workorders.py / _workorders_tab.html)
    ══════════════════════════════════════════ -->
    <div id="tab-workorders" class="tab-pane">
        {% include "_workorders_tab.html" %}
    </div>
```

**Replace with:**

```html
    <!-- ══════════════════════════════════════════
         TABS: Fault Report / Tech Support / Manage KB
    ══════════════════════════════════════════ -->
    <div id="tab-fault_report" class="tab-pane">
        {% include "_fault_report_tab.html" %}
    </div>
    <div id="tab-tech_support" class="tab-pane">
        {% include "_tech_support_tab.html" %}
    </div>
    {% if role == 'admin' or role == 'field_manager' %}
    <div id="tab-kb_admin" class="tab-pane">
        {% include "_kb_admin_tab.html" %}
    </div>
    {% endif %}
```

## Notes

- The legacy `_workorders_tab.html` is unchanged on disk. Patch 2 just stops including it. If you ever need to roll back, restore both lines and the V1 routes will still serve it (delivery + manager view + complaints + joborders).
- The `{% if role == 'admin' or role == 'field_manager' %}` Jinja guards mirror the same pattern already used for Locations/Dispatch.
- After applying both patches, hard-reload the browser to bust the cached page.
