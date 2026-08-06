document.addEventListener('DOMContentLoaded', function () {
  // Find the "Operating Hours" section by its heading text
  var sectionTitles = document.querySelectorAll('.clinic-info-section-title');
  var hoursSection = null;

  sectionTitles.forEach(function (title) {
    if (title.textContent.trim() === 'Operating Hours') {
      hoursSection = title.closest('.clinic-info-section');
    }
  });

  if (!hoursSection) return;

  // Within that section, find the paragraph that starts with "Open:"
  var detailLines = hoursSection.querySelectorAll('.clinic-info-detail');

  detailLines.forEach(function (line) {
    // Guard: skip if this line was already processed (prevents the script
    // from re-splitting its own output if the file is accidentally loaded
    // or run more than once on the same page).
    if (line.classList.contains('clinic-info-days-row')) return;

    var fullText = line.textContent.trim();
    if (fullText.indexOf('Open:') !== 0) return;

    var daysText = fullText.slice('Open:'.length).trim();
    var days = daysText
      .split(',')
      .map(function (day) { return day.trim(); })
      .filter(Boolean);

    if (days.length === 0) return;

    // Rebuild the line as a label + individual day badges
    line.textContent = '';
    line.classList.add('clinic-info-days-row');

    var label = document.createElement('span');
    label.className = 'clinic-info-days-label';
    label.textContent = 'Open:';
    line.appendChild(label);

    days.forEach(function (day) {
      var badge = document.createElement('span');
      badge.className = 'clinic-info-day-badge';
      badge.textContent = day;
      line.appendChild(badge);
    });
  });
});