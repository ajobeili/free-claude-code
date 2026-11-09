// Only the selected workspace may issue background API requests.
(() => {
  const module = ({'/workbench':'workbench','/structure':'structure','/planner':'planner','/pattern':'pattern'})[location.pathname] || 'home';
  const originalFetch = window.fetch.bind(window);
  let ready = Promise.resolve();
  function activate() {
    if (document.hidden) return;
    ready = originalFetch('/api/module?module=' + module, {method:'POST'}).catch(() => null);
    localStorage.setItem('fcc-active-module', module);
  }
  window.fetch = async (input, options = {}) => {
    const url = new URL(input instanceof Request ? input.url : input, location.href);
    if (url.origin !== location.origin || !url.pathname.startsWith('/api/')) return originalFetch(input, options);
    await ready;
    const shared = ['/api/login','/api/logout','/api/session'].includes(url.pathname);
    if (!shared && (document.hidden || localStorage.getItem('fcc-active-module') !== module)) {
      return new Response(JSON.stringify({detail:'Module paused. Return to this module to resume.'}), {status:409,headers:{'Content-Type':'application/json'}});
    }
    const headers = new Headers(options.headers || (input instanceof Request ? input.headers : undefined));
    headers.set('X-FCC-Module', module);
    return originalFetch(input, {...options, headers});
  };
  window.addEventListener('focus', activate);
  document.addEventListener('visibilitychange', () => {if (!document.hidden) activate();});
  activate();
})();

