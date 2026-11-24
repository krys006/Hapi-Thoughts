# Income summary v2 — decisions (approved by Krys, 2026-11-24)

Mockup: `docs/mockups/income_v2.html` (frames 1–8, approved 2026-11-24). Replaces v1 (not kept in the repo).

## Scope
1. A read-only admin page, **Income**. Totals come only from existing **Paid** bills. No new model, no migration, no charts library, no JavaScript of its own, no exports (CSV/PDF), no printing, no writes (GET only).
2. FEATURES.md: new section **5.4 Income summary (Admin)**; the Out of Scope line becomes "Reporting and analytics module (beyond the read-only Income summary in 5.4)"; the "Reporting module" expansion row notes that exports, custom date ranges and per-service / per-owner / per-pet breakdowns stay future work.

## What counts
3. **Money received** = Sum(`total_amount`) of PAID bills (after discounts).
4. A bill counts on its **paid day** = `payment_date`, or `billing_date` when `payment_date` is empty (same rule as the owner Bills History).
5. **Bills paid** = number of PAID bills (a ₱0 bill with items counts, adds ₱0).
6. **Discounts given** = Sum(`subtotal` − `total_amount`) and the number of PAID bills where `subtotal` > `total_amount`.
7. **From visits** = PAID bills with a visit (`appointment` set); **Over the counter** = the rest. The two add up to Money received.
8. **Not paid yet** (side box): all PENDING bills with at least one item, count and Sum(`total_amount`), a snapshot (not tied to the period), "Not counted in money received. Shows how things stand right now.", link "See unpaid bills" to the Billing Unpaid tab.
9. Cancelled bills are left out entirely.

## Periods
10. Segmented switch **Day · Week · Month · Year**; **Previous** / period name / **Next** in the same strip. Next is grayed (not a link) on the current period. "Back to today" (same kind, no date) only away from the current period.
11. Opens on **Day, today**. Week = Monday to Sunday.
12. URL: `?period=day|week|month|year&date=YYYY-MM-DD`. Unknown period → day; bad date, a date after today or before 2000-01-01 → today. The date is moved to the start of its period (Monday, 1st of the month, Jan 1). No Previous link when the previous period would start before 2000-01-01.
13. No custom date range (expansion point).

## Comparison
14. One line in the hero pill: current period "Yesterday:", "Last week:", "Last month:", "Last year:"; a past period "The day before (Sat, Nov 21):", "The week before (Nov 9 to 15):", "The month before (September 2026):", "The year before (2024):". Then "₱X from N bills", or "no bills paid". No percentages, arrows or green/red.

## Layout (frames 1–5)
15. Two columns: main (hero card, three tiles, then the bill list or the table) and a side column 22rem wide, sticky: the **compact chart** then **Not paid yet**. Phone: one column, chart after the tiles.
16. Hero: violet card (`from-violet-800 to-violet-600`, white text) "Money received · {when}", amount `text-5xl`, "from N bills paid" or "No bills were paid.", the comparison pill. It is a heading, not a button. The page has **no violet button**.
17. Tiles (only when bills were paid): From visits (stethoscope), Over the counter (pill), Discounts given (percent; "None" when 0, else "₱X" + "on N bills").
18. Day view: "Bills paid on this day (N)", oldest bill first; each row bill number, "From a visit" / "Over the counter" (+ "· ₱X off" with a discount), owner link, "for {pet}" or "Clinic bill", total, light-violet Open; footer "Total · N bills" + amount. No bills: empty card "No bills were paid on this day" / "Look at the day before, or at the whole week."
19. Week / Month / Year: table "Day by day" / "Month by month" (Day or Month · Bills paid · Money received), each row a link (a day to its Day view, a month to its Month view), the current day or month tinted with a tag ("Today" / "So far"), rows after today say "Not yet" (no link), a past row with nothing "No payments" + ₱0.00, footer Total.

## Chart (frames 1–8)
20. Plain HTML bars, heights worked out on the server, no library, no new JavaScript. The chart is a picture of the table (`aria-hidden`); screen readers use the table / the list.
21. Which bars: Day → the week that day is in (that day marked "Today" or "This day"); Week → its 7 days; Month → each day; Year → 12 months. Bars: violet-300 (compact) / violet-400 (big); the marked one violet-700; after today a small dashed box ("Not yet"); ₱0 a thin gray line.
22. Scale top = a round number above the highest bar: ceil(max / 10^k × 2) / 2 × 10^k (k = digits of max − 1); ₱1,000 when everything is 0. Axis labels in whole pesos (top and ₱0 compact; top, half, ₱0 big).
23. Compact chart: a card that is one `<button>` (spans inside) opening the big chart dialog; title ("This week, day by day", "That week, day by day", "October, day by day", "2026, month by month"), a sub line with the dates, the bars, labels under them (week: Mon…Sun; month: 1, 10, 20, last day; year: Jan, Apr, Jul, Oct), and the line "See the chart bigger" with the `maximize-2` icon.
24. Big chart: native `<dialog>` with the shared `dialogs.js` (`data-dialog-open`), Close button with `autofocus`. Title "Money received each day" / "…each month" + dates; four tiles: Money received (+ bills), Best day / Best month (+ its date), Average per day / per month (only days or months with payments), Days / Months with payments ("N of M" up to today); tall bars with the exact amount on each bar (week and year; **not** on the month, with the line "With 31 days the amounts would be too small to read on the bars, so they are in the table on the page."); labels under each bar (week "Mon" + "Nov 23"; month the day number; year "Jan"); a dashed amber line at the average with "Average ₱X"; a key in words (Money received, the marked bar's word, Average, Not yet when shown).

## Placement
25. Sidebar link **Income** right under **Billing** (`banknote` icon). URL `/admin/income/`, name `admin_income`, in the `billing` app; admin only; GET only; view under 50 lines, helpers in `billing/utils.py`.
26. Title "Income", subtitle "Money received from paid bills. Payment is cash." Dashboard unchanged.
27. New icons, copied from `lucide-static@1.66.0`: `banknote`, `maximize-2`. Already there: `stethoscope`, `pill`, `percent`, `clock`, `calendar`, `receipt`.

## Batches
- **I-1 backend**: period rules, sums, rows, chart data, context, view, URL, a plain template, tests, FEATURES / HANDOFF / CONVENTIONS lines.
- **I-2 page**: the approved design (layout, hero, tiles, list, table, compact chart, big chart dialog, phone), sidebar link, two icons, Tailwind rebuild.
