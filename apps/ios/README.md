# Persona_ize for iPhone

iOS 16 이상용 SwiftUI 앱과 보조 키보드 확장이다. 앱에서 계정으로 로그인하고 등록된 상대를 골라 합성 초안을 첨삭한다. 추천을 검토한 뒤 ‘키보드에 준비’를 누르면 확장이 읽을 문장 한 개를 앱 그룹에 저장한다. 메신저에서 지구본으로 키보드를 전환하고 ‘준비한 문장 넣기’를 누르면 현재 커서에 삽입한다. 전체 접근 권한은 필요 없다. 앱의 ‘준비한 문장 지우기’로 저장본을 삭제한다.

## 실기기 설치

1. iOS SDK가 설치된 Xcode에서 PersonaIze.xcodeproj를 연다. 이미 생성된 프로젝트를 사용하면 XcodeGen 설치는 필요 없다.
2. 프로젝트 Build Settings에서 PERSONA_BUNDLE_ID를 본인 팀의 고유 식별자로 지정한다. 기본값은 com.skywith628.personaize다. 키보드 식별자는 이 값에 .keyboard를 붙인다.
3. 두 타깃의 Signing & Capabilities에서 같은 Team을 선택하고 자동 서명을 켠다. Apple Developer 계정에 두 App ID와 group.<앱 번들 ID>를 등록하고 양쪽에 같은 App Group을 연결한다. 양쪽 서명 프로파일에 해당 그룹이 포함되어야 한다.
4. PersonaIze 스킴과 연결된 아이폰을 선택해 Run한다. 로그인 화면에 기기에서 접근 가능한 신뢰된 HTTPS 백엔드 주소를 입력한다. 백엔드에는 시연 계정과 합성 데이터로 만든 상대와 저장된 페르소나가 있어야 한다. 비밀번호와 서버 세션은 앱을 종료하면 다시 입력한다.
5. 설정 → 일반 → 키보드 → 키보드 → 새로운 키보드 추가에서 Persona_ize를 선택한다. 메모 앱에서 추천 문장 삽입과 원래 키보드로 전환을 확인한다. 원문 유지 응답, 네트워크 오류 후 같은 초안 재시도, 준비 문장 삭제도 확인한다.

## TestFlight

배포에 사용할 Xcode와 Apple Developer Program 팀에서 App Store Connect 앱 레코드를 같은 Bundle ID로 생성한다. 프로젝트 버전과 빌드 번호를 정하고 PersonaIze 스킴의 기기 대상을 Any iOS Device로 선택한다. Product → Archive 후 Organizer에서 Validate App과 Distribute App → App Store Connect를 진행한다. 처리 완료 후 TestFlight에서 테스트 그룹에 빌드를 배정한다.

프로젝트 설정 원본은 project.yml이다. 타깃 구성을 바꾸면 이 디렉터리에서 xcodegen generate로 프로젝트를 다시 생성한다. Xcode에서 직접 설정한 팀이나 식별자를 유지하려면 재생성 전에 project.yml에 반영한다.
