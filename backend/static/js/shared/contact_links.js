/*
  My details, Social and other contacts (owner pages). The contact links
  formset renders one row per saved link plus two empty rows, all visible.
  With JavaScript the empty rows hide, "Add a link" and each row's remove
  button show, and:
  - Add a link shows the next hidden row and moves the focus into it;
  - Remove empties the row and hides it (an empty row is dropped on save).
  Markup: [data-links] holds [data-link-row] rows, each with a
  [data-link-remove] button, and one [data-link-add] button. A row with a
  problem ([data-link-error]) stays visible. Without JavaScript every row
  shows and a link is removed by clearing its fields.
*/
(function () {
  "use strict";

  function fields(row) {
    return row.querySelectorAll("input:not([type=hidden])");
  }

  function isBlank(row) {
    return Array.prototype.every.call(fields(row), function (input) {
      return !input.value.trim();
    });
  }

  // "Add a link" shows only while an empty row is left to open
  function refresh(card) {
    var add = card.querySelector("[data-link-add]");
    if (add) add.hidden = !card.querySelector("[data-link-row][hidden]");
  }

  function setup(card) {
    card.querySelectorAll("[data-link-row]").forEach(function (row) {
      var remove = row.querySelector("[data-link-remove]");
      if (remove) remove.hidden = false;
      row.hidden = isBlank(row) && !row.hasAttribute("data-link-error");
    });
    refresh(card);
  }

  document.addEventListener("click", function (event) {
    var button = event.target.closest("[data-link-remove], [data-link-add]");
    if (!button) return;
    var card = button.closest("[data-links]");
    if (!card) return;
    if (button.matches("[data-link-remove]")) {
      var row = button.closest("[data-link-row]");
      fields(row).forEach(function (input) {
        input.value = "";
      });
      row.removeAttribute("data-link-error");
      row.hidden = true;
    } else {
      var next = card.querySelector("[data-link-row][hidden]");
      if (next) {
        next.hidden = false;
        var first = fields(next)[0];
        if (first) first.focus();
      }
    }
    refresh(card);
  });

  document.querySelectorAll("[data-links]").forEach(setup);
})();
