function getCsrfToken(){var m=document.cookie.match(/(?:^|; )csrf_token=([^;]*)/);return m?decodeURIComponent(m[1]):"";}
document.addEventListener("htmx:configRequest",function(e){e.detail.headers["X-CSRF-Token"]=getCsrfToken();});

(function(){
  var dd = document.querySelector('.nav-dropdown');
  if (!dd) return;
  var btn = dd.querySelector('.nav-dropdown__toggle');
  btn.addEventListener('click', function(e) {
    e.stopPropagation();
    var open = dd.classList.toggle('open');
    btn.setAttribute('aria-expanded', open);
  });
  document.addEventListener('click', function() {
    dd.classList.remove('open');
    btn.setAttribute('aria-expanded', 'false');
  });
})();
