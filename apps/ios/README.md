# Persona_ize for iPhone

iOS 16 이상용 SwiftUI 앱과 보조 키보드 확장이다. 앱은 탭 네 개로 나뉜다. 작성 탭에서 상대를 골라 합성 초안을 첨삭하고, 상대 탭에서 상대 등록·페르소나 편집·합성 대화 가져오기를 하며, 이력 탭에서 최근 첨삭을 다시 보고, 계정 탭에서 사용량 확인·비밀번호 변경·로그아웃·계정 삭제를 한다. 추천을 검토한 뒤 ‘키보드에 준비’를 누르면 확장이 읽을 문장 한 개를 앱 그룹에 저장한다. 메신저에서 지구본으로 키보드를 전환하고 ‘준비한 문장 넣기’를 누르면 현재 커서에 삽입한다. 키보드 설정에서 ‘전체 접근 허용’을 켜면 앱을 거치지 않고 키보드 맨 윗줄의 ‘○○에게 맞게’로 입력창의 초안을 바로 다듬어 바꿔 넣는다(버튼을 누를 때만 입력창을 읽고, 전송은 하지 않는다). 이때 키보드는 앱이 앱 그룹에 둔 로그인 세션을 쓰므로 앱에서 먼저 로그인해야 한다. 켜지 않으면 준비한 문장 넣기만 된다. 계정 탭의 ‘준비한 문장 지우기’로 저장본을 삭제한다. 서버 주소와 아이디만 기기에 기억하고 비밀번호와 세션은 저장하지 않는다.

단위 테스트는 `xcodebuild -project PersonaIze.xcodeproj -scheme PersonaIze -destination 'platform=iOS Simulator,name=iPhone 17 Pro' test`로 돌린다. 명령줄에서 실기기로 빌드할 때는 `DEVELOPMENT_TEAM=<팀 ID> -allowProvisioningUpdates`를 붙인다. App Group은 Xcode의 Signing & Capabilities에서 Team을 한 번 선택해야 등록된다.

## 실기기 설치

1. iOS SDK가 설치된 Xcode에서 PersonaIze.xcodeproj를 연다. 이미 생성된 프로젝트를 사용하면 XcodeGen 설치는 필요 없다.
2. 프로젝트 Build Settings에서 PERSONA_BUNDLE_ID를 본인 팀의 고유 식별자로 지정한다. 기본값은 com.skywith628.personaize다. 키보드 식별자는 이 값에 .keyboard를 붙인다.
3. 두 타깃의 Signing & Capabilities에서 같은 Team을 선택하고 자동 서명을 켠다. Apple Developer 계정에 두 App ID와 group.<앱 번들 ID>를 등록하고 양쪽에 같은 App Group을 연결한다. 양쪽 서명 프로파일에 해당 그룹이 포함되어야 한다.
4. PersonaIze 스킴과 연결된 아이폰을 선택해 Run한다. 로그인 화면에 기기에서 접근 가능한 신뢰된 HTTPS 백엔드 주소를 입력한다. 백엔드에는 시연 계정과 합성 데이터로 만든 상대와 저장된 페르소나가 있어야 한다. 비밀번호와 서버 세션은 앱을 종료하면 다시 입력한다.
5. 설정 → 일반 → 키보드 → 키보드 → 새로운 키보드 추가에서 Persona_ize를 선택한다. 메모 앱에서 추천 문장 삽입과 원래 키보드로 전환을 확인한다. 원문 유지 응답, 네트워크 오류 후 같은 초안 재시도, 준비 문장 삭제도 확인한다.

## TestFlight

배포에 사용할 Xcode와 Apple Developer Program 팀에서 App Store Connect 앱 레코드를 같은 Bundle ID로 생성한다. 프로젝트 버전과 빌드 번호를 정하고 PersonaIze 스킴의 기기 대상을 Any iOS Device로 선택한다. Product → Archive 후 Organizer에서 Validate App과 Distribute App → App Store Connect를 진행한다. 처리 완료 후 TestFlight에서 테스트 그룹에 빌드를 배정한다.

프로젝트 설정 원본은 project.yml이다. 타깃 구성을 바꾸면 이 디렉터리에서 xcodegen generate로 프로젝트를 다시 생성한다. Xcode에서 직접 설정한 팀이나 식별자를 유지하려면 재생성 전에 project.yml에 반영한다.
