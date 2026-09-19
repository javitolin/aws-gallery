// Redirect-only. CloudFront still validates the signed cookies itself, so a forged
// gallery_exp buys nothing but a 403 one hop later.
function handler(event) {
  var request = event.request;

  var expiry = request.cookies['gallery_exp'];
  if (expiry && parseInt(expiry.value, 10) * 1000 > Date.now()) {
    return request;
  }

  // querystring is an object here, not a string — concatenating it yields "[object Object]".
  var pairs = [];
  for (var key in request.querystring) {
    var param = request.querystring[key];
    pairs.push(param.value ? key + '=' + param.value : key);
  }
  var next = request.uri + (pairs.length ? '?' + pairs.join('&') : '');

  return {
    statusCode: 302,
    statusDescription: 'Found',
    headers: {
      location: { value: '/auth/login?next=' + encodeURIComponent(next) },
      'cache-control': { value: 'no-store' }
    }
  };
}
