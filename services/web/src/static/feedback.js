(function() {
  var btn = document.getElementById('btn-show-wrong');
  var form = document.getElementById('wrong-class-form');
  if (btn && form) {
    btn.addEventListener('click', function() {
      form.classList.add('open');
      btn.style.display = 'none';
      var input = form.querySelector('input[type="text"]');
      if (input) input.focus();
    });
    if (form.classList.contains('open')) {
      btn.style.display = 'none';
    }
  }
})();
