(function() {
  var evtSrc = new EventSource('/deterrent-log/stream');
  evtSrc.addEventListener('actuation', function(e) {
    var tbody = document.getElementById('actuations-body');
    if (!tbody) return;
    var empty = tbody.querySelector('td[colspan]');
    if (empty) empty.parentElement.remove();
    var tmp = document.createElement('tbody');
    tmp.innerHTML = e.data;
    var row = tmp.firstElementChild;
    if (row) tbody.insertBefore(row, tbody.firstChild);
  });
})();
