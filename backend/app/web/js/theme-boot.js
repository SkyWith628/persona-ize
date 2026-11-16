// 저장해 둔 테마(밝게/어둡게)를 첫 그리기 전에 적용한다 — 다른 테마가 잠깐 번쩍이지 않게.
// 모듈 스크립트는 문서를 다 읽은 뒤 실행되므로, 이 파일만 <head>에서 일반 스크립트로 먼저 불러온다.
// (인라인 스크립트는 쓰지 않는다: CSP script-src 'self'.) 키 이름은 js/store.js의 THEME_KEY와 같다.
(function () {
  try {
    var theme = window.localStorage.getItem("pz_theme");
    if (theme === "light" || theme === "dark") document.documentElement.setAttribute("data-theme", theme);
  } catch (err) {
    /* 저장소를 쓸 수 없으면 시스템 설정을 따른다 */
  }
})();
