document.addEventListener('DOMContentLoaded', function () {
  var form = document.querySelector('.settings-main form');
  if (!form) return;

  // Guard: don't rebuild if this has already run once on this page
  if (document.querySelector('.settings-tabs')) return;

  var sections = Array.prototype.slice.call(form.querySelectorAll('.settings-section'));
  if (sections.length < 3) return;

  // Match each section by its title text — more robust than assuming
  // DOM order, since the desired tab order differs from source order.
  function findSection(titleText) {
    return sections.find(function (section) {
      var title = section.querySelector('.settings-section-title');
      return title && title.textContent.trim() === titleText;
    });
  }

  var clinicInfoSection = findSection('Clinic Information');
  var vetProfileSection = findSection('Veterinarian Profile');
  var scheduleSection = findSection('Schedule Configuration');

  if (!clinicInfoSection || !vetProfileSection || !scheduleSection) return;

  // Blocked Dates lives in .settings-sidebar with its own separate HTMX
  // form — moving it to be a literal child INSIDE the main settings
  // <form> would nest one form inside another (invalid HTML, same bug
  // class as the walk-in form's duplicate-<form> issue earlier). Instead
  // it moves to sit as a SIBLING right after the main form, and its
  // visibility is just synced with the Schedule tab being active.
  var blockedDatesSection = null;
  document.querySelectorAll('.settings-sidebar .settings-section').forEach(function (section) {
    var title = section.querySelector('.settings-section-title');
    if (title && title.textContent.trim() === 'Blocked Dates') {
      blockedDatesSection = section;
    }
  });

  var tabs = [
    { label: 'Clinic Info', section: clinicInfoSection },
    { label: 'Schedule', section: scheduleSection },
    { label: 'Vet Profile', section: vetProfileSection },
  ];

  var scheduleTabIndex = 1;

  // Build the tab nav bar
  var tabNav = document.createElement('div');
  tabNav.className = 'settings-tabs';

  tabs.forEach(function (tab, index) {
    var button = document.createElement('button');
    button.type = 'button';
    button.className = 'settings-tab' + (index === 0 ? ' settings-tab--active' : '');
    button.textContent = tab.label;
    button.addEventListener('click', function () {
      activateTab(index);
    });
    tabNav.appendChild(button);
    tab.button = button;
  });

  // Reorder the actual section elements to Clinic Info / Schedule / Vet
  // Profile order, and insert the tab nav right before the first one.
  form.insertBefore(tabNav, clinicInfoSection);
  tabs.forEach(function (tab) {
    form.appendChild(tab.section);
  });

  // Move Blocked Dates to sit as a sibling right after the form (not
  // inside it — see note above on why), so it visually appears "in"
  // the Schedule tab without nesting its form inside the settings form.
  if (blockedDatesSection) {
    form.parentNode.insertBefore(blockedDatesSection, form.nextSibling);
    blockedDatesSection.classList.add('blocked-dates-in-schedule-tab');
  }

  // .settings-sidebar was only ever holding Blocked Dates — now that
  // it's moved out, the sidebar column is empty and would otherwise
  // leave a blank gap in the two-column grid layout.
  var sidebar = document.querySelector('.settings-sidebar');
  if (sidebar) {
    sidebar.style.display = 'none';
  }
  var settingsLayout = document.querySelector('.settings-layout');
  if (settingsLayout) {
    settingsLayout.classList.add('settings-layout--single-column');
  }

  function activateTab(activeIndex) {
    tabs.forEach(function (tab, index) {
      var isActive = index === activeIndex;
      tab.section.style.display = isActive ? '' : 'none';
      tab.button.classList.toggle('settings-tab--active', isActive);
    });

    if (blockedDatesSection) {
      blockedDatesSection.style.display = activeIndex === scheduleTabIndex ? '' : 'none';
    }
  }

  // Show only the first tab initially
  activateTab(0);

  /* ---- Move "Save Settings" to the top-right header ----
     The button stays a real submit button for the same form — using
     the form="..." attribute lets it submit correctly even though it
     no longer lives inside the <form> element's DOM subtree. This is
     the standards-compliant way to do this, unlike moving actual form
     elements around (which is what caused the nested-form problem
     avoided above for Blocked Dates). */
  var saveButton = form.querySelector('.form-actions button[type="submit"]');
  var header = document.querySelector('.admin-page-header');

  if (saveButton && header) {
    if (!form.id) {
      form.id = 'clinic-settings-form';
    }
    saveButton.setAttribute('form', form.id);

    // Group the existing title + subtitle together so the header can
    // become a flex row (title group on the left, button on the right)
    // without the button splitting in between them as a third item.
    var titleGroup = document.createElement('div');
    var title = header.querySelector('.admin-page-title');
    var subtitle = header.querySelector('.admin-page-subtitle');
    if (title) titleGroup.appendChild(title);
    if (subtitle) titleGroup.appendChild(subtitle);

    header.innerHTML = '';
    header.appendChild(titleGroup);
    header.appendChild(saveButton);
    header.classList.add('admin-page-header--with-action');
  }
});