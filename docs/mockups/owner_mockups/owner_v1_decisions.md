# Owner side v1 — decisions (Slice 8, settled 2026-11-16)

Settled with Krys after the Slice 8 audit. Read with `OWNER_MOCKUPS.md` (look and
layout). Where this file and a mockup disagree, this file wins. The md files
(FEATURES, DATA_MODELS, CONVENTIONS) are updated to match.

## Decisions

| # | Topic | Decision |
|---|---|---|
| 1 | Cancelled bills | Owners never see cancelled receipts (unchanged rule; 404 stays). The cancelled frame in `owner_bills_v1.html` is dropped. |
| 2 | Breed and birthday | Optional on every pet form (FEATURES, DATA_MODELS, admin form). The "required" stars in the owner mockups are wrong. |
| 3 | Owner email | Only the clinic changes an owner's email. My details shows it read-only with "To change your email, contact the clinic." An owner email-change flow is a future expansion point. |
| 4 | Terms and Privacy | Keep the two links on Create account as placeholders. Their URLs come from one place (`settings.TERMS_URL` / `settings.PRIVACY_URL`, today's external links as the values) so real pages can replace them with one change. Real clinic pages are a before-go-live item. |
| 5 | Owner reschedule | Keep today's behaviour for v1: the current visit is cancelled and a new PENDING request is made, in one transaction. The Change time sheet says so plainly ("Your {time} time is given up. The new time waits for the clinic to confirm."). Keeping the original until approval is a future expansion point. |
| 6 | Resend verification link | Yes: "Send the link again" on Check your email and on login for an unverified account. At most one send per 2 minutes per account (session/cache timestamp, no migration). |
| 7 | Vaccination corrections | Owners see only "Corrected by the clinic", never `correction_note`. |
| 8 | Test result files | Text only for owners ("On file. Ask the clinic for a copy."), no file links, while files are public URLs. |
| 9 | Old notifications | No rewrite. Old rows keep their long text; new ones use the compact text. |
| 10 | Logout | POST only, through the log-out sheet. GET no longer logs out. |
| 11 | allauth pages | `/accounts/signup/` and `/accounts/login/` redirect to the owner register/login pages; the Google provider and callback URLs keep working. |
| 12 | Shared files | `dialogs.js` moves to `static/js/shared/` (both layouts load it). The flash style (colour + icon + bold lead words) changes in the shared `components/flash_messages.html`; admin pages are checked. |
| 13 | Dashboard polling | Dropped. The dashboard is one normal page. |
| 14 | Pet removal wording | Owners "remove a pet"; the model and admin keep "deletion request". UI wording only. |
| 15 | Take back a removal | Done properly in this slice: a CANCELLED status on PetDeletionRequest (choices-only migration, batch 8a-0). No interim hard delete. |

## Also decided

- **"Declined" reason:** new `CANCEL_CHOICES` value `declined`, set when the clinic declines a request (choices-only migration, 8a-0). Owners see "Declined by the clinic" vs "Cancelled by the clinic". Old rows are not backfilled.
- **Booking race:** owner booking and owner reschedule lock the one ClinicSettings row (`select_for_update()`) inside a transaction, re-check the slot, then save. No migration. A DB constraint stays in the backlog.
- **Contact links:** replacing them on a profile save is allowed (owner settings, not clinic history) but runs in one transaction.
- **Owner pet edit:** name, breed, color, weight, photo only. Species, sex, birthday and registration no. are shown locked ("Ask the clinic to change these.").

## Bugs fixed first (8a-1)

1. Owner views crash (500) for a user with no PetOwner row: one helper sends them to onboarding.
2. Onboarding step 1 crashes on a second submit (OneToOne IntegrityError).
3. A failed verification email gives a 500 after the account is saved: log it, show a plain message.
4. Owner pet edit allows locked fields.
5. Owner reschedule cancel-then-create isn't atomic.

## Future expansion points (add to FEATURES)

Keep the original visit until an owner reschedule is approved; limit open requests per owner; owner email change; late-cancellation approval (already planned); owner calendar page; split dashboard loading; printing a medical record for owners; prices on the booking page (already planned); owner account deletion / data export.

## Batches

- **8-docs:** this file into the repo, FEATURES, OWNER_MOCKUPS.md, HANDOFF §5.
- **8a-0 (`model:`):** the two choices-only migrations (plan approved first).
- **8a-1:** the five bugs, booking lock, atomic contact links, take back as CANCELLED, declined reason in use, logout POST, allauth redirects, Terms/Privacy settings.
- **8a-2:** `is_within_24h`, compact notification text, fixed preference choices, follow-up booked, calendar open/full/closed, Add pet `next`, bills `?q=&period=`, dashboard next visit + unpaid, new views (one record, notifications page, account hub, resend verification). May split.
- **8b-1 … 8b-10:** owner layout + components + error pages; auth; onboarding; dashboard + My Pets; pet profile/edit/remove; booking; visits; record + bills; notifications + account; loading states + cleanup.
