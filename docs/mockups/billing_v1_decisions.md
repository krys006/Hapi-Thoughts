# Admin billing pages (Slice 6): decisions (approved 2026-11-06)

Source: the read-only Slice 6 audit (2026-11-06), Krys's answers (all recommendations accepted) and the approved mockups.
Mockups (all approved 2026-11-06), in `docs/mockups/`: `receipt_detail_v1.html`, `receipt_list_v1.html`, `receipt_new_v1.html`, `services_v1.html`.
No model field changes and no migration in Slice 6. Choice labels shown on the pages ("Unpaid", "Offered", "Percent off (%)", …) are set in forms/templates only, never in the models' `choices` (that would create a migration). Anything that needs a migration goes to the backlog below.

## Rules (backend)

### Creating receipts
1. **One receipt per visit** (the existing OneToOne). A second create for the same visit, or a double submit, opens the existing receipt with "A receipt already exists for this visit." Never a 500 (the IntegrityError is caught inside `transaction.atomic`, as with medical records). A **cancelled** receipt still holds its visit: that visit can't get another receipt (the cancel dialog says so).
2. **Which visits**: CONFIRMED or COMPLETED, not dated after today (Manila date), the same rule as medical records (5a-2). Otherwise back to the appointment page with a plain message. A receipt with **no visit** is allowed (over-the-counter supplies).
3. **Owner and pet**: an owner is always required. The pet is optional ("Not for a specific pet"); when given it must be one of that owner's active pets ("Choose one of {owner}'s pets."). From a visit, both come from the visit.
4. **Archived owner or pet**: creating is refused (GET and POST, "Restore {owner} first." / "Restore {pet} first."). An existing receipt can still be edited, paid or cancelled.
5. **Bad ids** (`?appointment=abc`, `?owner=abc`, a posted `pet` that isn't a whole number or doesn't exist): 404, never a 500. A posted pet that exists but isn't one of the owner's active pets gets the form message from rule 3.
6. A new receipt is saved **empty** and opens the receipt page; items are added there. Exception: from a visit whose service has a **fixed price**, a checkbox (ticked by default) adds that service as the first item at its price. Range-price or no service: no checkbox.
7. **Pet pre-pick** (not from a visit): an owner with exactly one active pet has it picked; two or more: nothing picked, and an empty choice is refused ("Choose a pet, or “Not for a specific pet”."); no pets: only "Not for a specific pet", picked.

### Money
8. **Discount**: a percentage over 100 is refused ("A discount can't be more than 100%."); a peso discount bigger than the subtotal is refused ("A discount can't be more than the subtotal (₱700.00)."); an amount with no discount type is refused ("Choose the kind of discount, or clear the amount."); a type with no amount: "Type how much."; negative amounts refused. The discount shown and printed is capped at the subtotal (removing an item later can shrink it), so the printed sum always adds up. A "Discount" row shows only when there is a discount.
9. **Rounding**: the discount and the total are rounded to 2 decimals (ROUND_HALF_UP) in `calculate_totals()`.
10. **Items**: quantity 1 to 999 ("The quantity must be from 1 to 999."); unit price 0 or more ("The price can't be negative."); a ₱0 item is allowed; one item's amount can't pass ₱999,999.99 (no 500 from the column limit).
11. **Range services**: any price can be billed; the help says "Usual price ₱300.00 to ₱800.00. You can bill outside it." A Fixed service pre-fills its price.
12. **"What is it?"**: "Choose one", the services (Offered + Clinic use only, with their prices), "Other (type it)" last. A picked service fills a blank description (and a blank price for a Fixed service) on the server, so it works without JS. Other with no description: "Type what it is." A blank price for Other or a Range service: "Type the price."
13. **Item order**: first added first (`order_by("created_at", "pk")` in the queries; no Meta change).

### Payment, cancel, lock
14. **Payment method**: always cash (core rule 5). The free-text field leaves the form; the column keeps saving "Cash". GCash / bank transfer recording → FEATURES "Known Future Expansion Points".
15. **Payment date**: today (Manila date) when marked paid; not editable.
16. **Billing date**: removed from the form; today when the receipt is created.
17. **Mark as paid**: refused for a receipt with no items ("Add at least one item first."). A ₱0 total with items is allowed.
18. **Cancel = void**. No reason field (migration → backlog). Can't be undone.
19. **Items removable while Unpaid**, then locked. Fix DATA_MODELS.md ("No deletion, locked with receipt").
20. **Lock race**: every status change, item change and discount change runs in a transaction with the receipt row locked (`select_for_update`) and acts only if the receipt is still PENDING; totals and status are saved with `update_fields`. No migration.
21. **Receipt numbers**: the next number is worked out numerically (not by string sort), so numbers past 9999 keep working. A receipt-number collision retries once. Year = the Manila year of creation (unchanged).
22. Every status change keeps a confirm dialog in plain words (native `<dialog>`, `dialogs.js`; no `openConfirmModal`, no `confirm()`).

### Owner side (backend and small template fixes only; the owner design waits for the owner-side slice)
23. **Owners see** Unpaid (badge "Unpaid") and Paid. Cancelled receipts are hidden from owners (list, detail = 404, owner search).
24. **One notification, at mark paid**: type `billing_generated` (kept), title "Receipt {number} for {pet}" (or "Receipt {number}" with no pet), message "Paid ₱450.00 on Fri, Nov 6. You can view or print it in Billing." Nothing on create. Fix FEATURES.md 5.3 / 6.2 / 6.5 and the preference wording ("Email when a receipt is paid.").
25. Owner receipt list: `select_related("pet")` (no N+1). Owner receipt page: the print partial moves to `{% block extra_js %}` so it is a direct child of `<body>`.

### Elsewhere
26. **Appointment page banner**: "All done for this visit" only when the receipt is PAID. Unpaid: "Receipt not paid yet" with a link to it. Cancelled: says the receipt was cancelled (Create receipt is not offered again).
27. **Closed services**: stay hidden from owners. Fix DATA_MODELS / FEATURES ("visible as unavailable").
28. `admin_receipt_create` under 50 lines (helpers in `billing/utils.py`). `LOCKED_MESSAGE` keeps its name (tests import it).

### Services
29. **Names are unique**, ignoring letter case and outer spaces: "There is already a service called “Consultation”." (form check, no migration).
30. **Saving clears the other pricing type**: One price clears min/max; a range sets `base_price` to the lowest price (the column has no blank) and needs both prices ("Type the lowest and the highest price."; "The highest price must be more than the lowest."). One price needs "Type the price."; "Prices can't be negative."
31. A new service starts as **Hidden (draft)** (as today). No delete; "No longer offered" retires one. Status labels on the pages: Offered (active), Clinic use only (unlisted), Hidden (draft) (draft), No longer offered (closed).

## Pages (approved mockups)
- **Receipt page** (`receipt_detail_v1.html`, 13 states). The only working page; the separate edit page goes (its URL redirects to the receipt page). Header: back to receipts, receipt tile, "Receipt {number}" + Unpaid (yellow) / Paid (green) / Cancelled (red) badge, "Billed to {owner} · for {pet}" (or "not for a specific pet"), the visit link or "Not linked to a visit". Banner per status. Left: Items (Item / Qty / Unit price / Amount, "From the price list" under service items, Remove per item while Unpaid) + totals. Right (sticky): Actions, Billed to, a small card (created, payment, what the owner sees). Violet button: Add item (empty), Mark as paid (Unpaid with items), Print receipt (Paid), none (Cancelled). Dialogs: Add item (errors reopen it with values kept), Discount, Remove item (says the new total), Mark as paid (amount, "in cash today", locks, who is told), Cancel receipt ("Keep it" focused; can't be undone, owner won't see it, the visit can't get another receipt). Unpaid receipts can be printed (stamped UNPAID). Printout: status stamp, Discount line only with a discount, pet line only with a pet; the partial stays a direct child of `<body>`.
- **Receipt list** (`receipt_list_v1.html`, 7 states). Header "Billing" + "New receipt" (violet). Top-bar search "Search receipts: number, owner or pet" (every word; number, owner first/last name, pet name), stays inside the tab. Tabs Unpaid (default; count yellow while > 0) · Paid · Cancelled · All (changed 2026-11-16: "Services and prices" is no longer a tab, see Services). `?status=pending|paid|cancelled|all` (the dashboard link keeps working). Sort: Unpaid oldest first, Paid newest paid first, Cancelled and All newest first. 25 per page, shared pager. Rows: receipt tile, number, "Created … · From the visit …" / "Over the counter" (Paid: "Paid …"), owner + pet, total ("No items yet" under ₱0.00), light-violet Open. Badges only on All. Cancelled: gray tile, struck-through total, gray note. Empty states per tab. No unpaid-money total (reporting is out of scope). Split pane and modal JS dropped.
- **New receipt** (`receipt_new_v1.html`, 8 states). From a visit: one summary card (visit + status, billed to, pet, service) + the fixed-price service checkbox, Cancel + Create receipt. From the list: step 1 "Who is it for?" (walk-in step 1 pattern: active owners A to Z, 25 per page, filter as you type on name / phone / pet name, top-bar search emptied, Choose per owner, no violet button), step 2 "Which pet?" (`?owner=`; Change owner; radio cards for each active pet + "Not for a specific pet" last; fieldset/legend written in the template), Cancel + Create receipt.
- **Services** (`services_v1.html`, 6 states). **Changed 2026-11-16 (option A of `billing_services_split_options.html`): its own sidebar link "Services and prices" (tag icon) in the Clinic group between Billing and Clinic settings (sidebar 9 → 10 links); page h1 "Services and prices", subtitle "The price list for receipts and for booking.", no Billing tabs; Billing is no longer highlighted on the services pages.** "Add service" (violet). One page grouped by status (Offered, Clinic use only, Hidden (draft), No longer offered), each with a one-line explanation and count, "None." when empty; rows: name + category pill, description or "No description", price + "One price" / "Price range", outline Edit; A to Z. Form: cards The service (Name, Category, Description), Price (One price / A price range radio cards, only that type's fields, indented; small script, both sets without JS), Who sees it (four radio cards); edit subtitle "Receipts already made keep their prices"; the "No longer offered" note on edit.

## Batches
- **6a-1** receipt rules: discount, rounding, item form, mark-paid rule, cash only, form fields removed, row locks, receipt numbers, item order (rules 8–10, 12–21)
- **6a-1** done 2026-11-07.
- **6a-2** create flow (rules 1–7, 28): from a visit / from an owner, pet rules, 404s, the service checkbox, billing_date set explicitly, create view under 50 lines, plain structural template `admin/billing/receipt_new.html` (design in 6b-3)
- **6a-3** receipt page + list contexts: receipt page context (incl. the range "Usual price" help, rule 11), list tabs / search / paging, dialog error re-renders, edit URL → receipt page
- **6a-4** owner notification, owner-side fixes, appointment banner, services backend (rules 23–27, 29–31)
(Split on 2026-11-07: the old 6a-2 was too big.)
- **6b-1** receipt list · **6b-2** receipt page + print · **6b-3** new receipt page · **6b-4** services list + form

## Docs to fix during the slice
DATA_MODELS (appointment OneToOne + related_name, billing_date default, defaults, item deletion, total clamp, discount rules, CLOSED services); FEATURES 5.1–5.3, 6.2, 6.5 (PDF = browser print, notification at payment, owners see Unpaid + Paid, CLOSED hidden); CONVENTIONS §3 billing tree (`billing_parts/`; `_billing_tabs.html` renamed since it isn't an HTMX partial), §4 removed files, §10 "print partial direct child of `<body>`" (owner page fixed); HANDOFF (split panes still ran on billing until 6b; FEATURES line ref).

## Backlog (not this slice)
- Cancel reason on receipts (new field → migration).
- Own notification type for "receipt paid" (choices → migration).
- Record GCash / bank transfer as a payment method (FEATURES expansion point).
- Admin global search doesn't search receipts (the receipt list has its own search).

## Before-live items carried from Slice 5 (do not lose before the deploy)
- Test result files are public URLs: decide.
- Read-only count on live of vaccinations wrongly flagged Corrected.
- The deploy: written migration plan from the `rebuild...trial-run` migrations diff, and a Supabase backup first.
