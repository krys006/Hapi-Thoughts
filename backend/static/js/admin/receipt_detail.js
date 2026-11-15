// Receipt page (admin/billing/receipt_detail.html). No inline handlers, no fetch.
//   [data-print]                 prints the receipt (window.print(); the page's
//                                @media print rules show only the receipt)
//   Add item "What is it?"       a picked service fills Description (its name) and
//                                Unit price (its price, Fixed only) when those boxes
//                                are empty or still hold the last auto-filled value,
//                                and shows the option's data-help under Unit price.
//                                "Other" and "Choose one" clear nothing.
//   Discount select              puts "%" or "₱" inside the How much box
(function () {
  "use strict";

  // Clicks once on document, like dialogs.js
  document.addEventListener("click", function (event) {
    if (event.target.closest("[data-print]")) window.print();
  });

  function setupAddItem() {
    var select = document.getElementById("id_service_choice");
    var name = document.getElementById("id_description");
    var price = document.getElementById("id_unit_price");
    var help = document.querySelector("[data-item-help]");
    if (!select || !name || !price) return;

    // What this script last typed, so a later pick may overwrite it but never
    // what the person typed
    var lastName = "";
    var lastPrice = "";

    select.addEventListener("change", function () {
      var option = select.options[select.selectedIndex];
      if (!option || !option.value || option.value === "other") {
        if (help) help.textContent = "";
        return;
      }
      var optionName = option.getAttribute("data-name") || "";
      var optionPrice = option.getAttribute("data-price") || "";
      if (name.value === "" || name.value === lastName) {
        name.value = optionName;
        lastName = optionName;
      }
      if (price.value === "" || price.value === lastPrice) {
        price.value = optionPrice;
        lastPrice = optionPrice;
      }
      if (help) help.textContent = option.getAttribute("data-help") || "";
    });
  }

  function setupDiscount() {
    var select = document.getElementById("id_discount_type");
    var sign = document.querySelector("[data-discount-sign]");
    var both = document.querySelector("[data-discount-both]");
    if (!select || !sign) return;

    var SIGNS = { percentage: "%", fixed: "₱" };

    function update() {
      var text = SIGNS[select.value] || "";
      sign.textContent = text;
      sign.classList.toggle("hidden", !text);
      sign.classList.toggle("flex", !!text);
      // Without a kind picked (or without this script) the label shows both
      if (both) both.classList.toggle("hidden", !!text);
    }

    select.addEventListener("change", update);
    update();
  }

  setupAddItem();
  setupDiscount();
})();
