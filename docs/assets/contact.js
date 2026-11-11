// "Get in touch" links (data-contact) lead to the Enterprise page's get-in-touch section. A click builds the address
// from its parts, so no page or file holds it whole for scrapers to collect, opens it in the mail client and shows it
// beside the link for anyone without one.
(function () {
  var address = ['enterprise', 'octoprox.com'].join('@');
  document.addEventListener('click', function (event) {
    var link = event.target instanceof Element && event.target.closest('a[data-contact]');
    if (!link || link.protocol === 'mailto:') return;
    link.href = 'mailto:' + address + '?subject=Octoprox%20Enterprise';
    var shown = document.createElement('span');
    shown.className = 'contact-address';
    shown.textContent = address;
    link.after(shown);
  });
})();
