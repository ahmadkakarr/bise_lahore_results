# Mobile egress application: selected technology baseline

Verified against primary release documentation and Google Maven metadata on
25 October 2026 (Asia/Karachi). This is a proposed implementation baseline, not
a claim that the combined application has already compiled or passed device tests.
Production dependencies use stable releases and exact pins, never dynamic `+`
versions or automatic preview upgrades.

## Android application

| Area | Selection | Purpose |
| --- | --- | --- |
| Platform | Android 17, compileSdk/targetSdk 37 | Current stable Android platform |
| Device floor | Proposed minSdk 26, Android 8.0 | Retain older consenting phones; validate physical models |
| Language | Kotlin 2.4.21 | Native Android and coroutine-based code |
| UI alignment | Compose stable BOM 2026.09.00 | Verified POM resolves UI/runtime/foundation 1.12.1 and Material 3 1.4.0 |
| Design | Material 3 1.4.0 | Accessible components, dark/light theme, dynamic colors |
| Navigation | Navigation 3 1.2.0 | Typed routes, back-stack state and per-entry ViewModels |
| Architecture | Unidirectional data flow, ViewModel, StateFlow | UI renders persisted domain/service state |
| Local structured DB | Room 3.0.3 | Device sessions, usage counters and bounded diagnostic history |
| DB generation | KSP, Room 3 Gradle plugin and exported schemas | Compile-time queries and migration validation |
| Settings | DataStore 1.3.0 | Owner preferences, schedules and consent settings |
| Dependency injection | Dagger Hilt 2.60.1; AndroidX Hilt 1.4.0 | Managed dependencies in service/ViewModels/maintenance workers |
| Control API | Retrofit 3.0.0, OkHttp 5.5.1, kotlinx.serialization | Typed REST plus authenticated connection/events |
| Async work | Kotlin coroutines and Flow | Structured cancellation, bounded concurrency, state updates |
| Active sharing | Android foreground service | User-visible tunnel lifecycle with Stop control |
| Maintenance | WorkManager 2.12.0 | Deferred cleanup/sync, not the permanent tunnel host |
| Network selection | Android Network callbacks, network-bound sockets and DNS | Explicit cellular/Wi-Fi selection without changing global routing |
| Secrets | Android Keystore-backed key storage | Device authentication and protected credentials |
| Tunnel foundation | FRP 0.71.1, thin Go/mobile wrapper | Reuse tunnel implementation; validate Android embedding physically |
| Build | AGP 9.4.1, Gradle wrapper 9.6.0, JDK 17 | Stable proposed build baseline with pinned native toolchain |

Room 3 uses `androidx.room3:room3-runtime` and `room3-compiler`, KSP and coroutine
APIs. Include its SQLiteDriver implementation explicitly and keep schema migration
tests. It is not simply changing the version of `androidx.room:room-runtime` to 3.
Use Room for structured records, DataStore for settings, and Keystore-backed
storage for credentials. Do not store credentials or applicant data in ordinary
settings/history. No destructive migration fallback for authoritative counters.

Navigation 3 has its own 1.x artifact version; the name does not mean version 3.x.
Use the Kotlin Compose compiler plugin aligned to the resolved Kotlin toolchain.
AGP/Kotlin/KSP/Hilt/native-wrapper compatibility must pass a clean build before
freezing the complete lockfile. Availability as stable individual libraries is
not proof of compatibility of the complete proposed matrix.

## Service behavior and reliability

Persist owner intent and service state independently of the UI; reopening a screen
must not start a second tunnel. Use explicit states and failure reasons, bounded
reconnect backoff with jitter, resource limits and transactional counter updates.
Separate network failures, server cooldowns and owner pauses. Clear only the
appropriate backoff after recovery. Respect owner stop and Android execution rules.

The foreground-service type remains a physical feasibility/review gate from the
main plan. WorkManager does not confer unrestricted background execution. FRP is
the transport foundation; enrollment, revocation, quotas and lifecycle supervision
are application responsibilities. Libraries do not supply those guarantees alone.

Keep control API retries separate from workload submission. Idempotent control
requests may retry with bounds; registration POSTs retain the existing durable
intent, uncertainty and duplicate handling instead of generic automatic replay.

## Server and existing integration

Retain the earlier scoped server plan: FRP gateway, Python FastAPI control API,
SQLite WAL with a single registry writer for V1, and bounded structured logs.
Move the registry to PostgreSQL if multi-instance control-plane requirements
justify it; do not migrate the current applicant ledger merely for stack symmetry.
The existing registration engine remains Python/HTTPX with its authoritative
SQLite completion ledger, receipts and tracking IDs. Android Room is not a second
source of truth for registration completion.

Build the Android/networking service as a reusable project. This BISE checkout
gets a managed-device provider adapter. Device discovery and per-device statistics
extend the existing monitor; they do not replace the registration engine.

## Verification and maintainability

- JUnit/AndroidX Test, coroutine test utilities, Turbine, MockWebServer and Compose
  UI tests for state, API errors, lifecycle and UI behavior.
- Room schema/migration tests, consent/revocation and concurrent counter tests.
- Physical Samsung plus another OEM: screen-off, battery saver, process eviction,
  network switching, data caps, reconnect, owner stop and overnight soak.
- Cross-language tunnel cancellation, bounded buffers and original exit checks.
- Gradle version catalog, dependency locking/checksum verification, pinned native
  dependencies, license notices, lint/static analysis, R8 release-build testing.
- Report a device-test result as such; local unit success is not proof of phone
  background reliability. Reconnect timing and battery/data cost need measurements.

## Sources

- Android SDK 37: https://developer.android.com/about/versions/17/setup-sdk
- Kotlin: https://kotlinlang.org/docs/releases.html
- Compose: https://developer.android.com/jetpack/androidx/releases/compose
- Stable BOM metadata/POM checked directly:
  https://dl.google.com/dl/android/maven2/androidx/compose/compose-bom/maven-metadata.xml
  https://dl.google.com/dl/android/maven2/androidx/compose/compose-bom/2026.09.00/compose-bom-2026.09.00.pom
- Material 3: https://developer.android.com/jetpack/androidx/releases/compose-material3
- Navigation 3: https://developer.android.com/jetpack/androidx/releases/navigation3
- Room 3: https://developer.android.com/jetpack/androidx/releases/room3
- DataStore: https://developer.android.com/jetpack/androidx/releases/datastore
- WorkManager: https://developer.android.com/jetpack/androidx/releases/work
- Hilt: https://dagger.dev/hilt/gradle-setup
  https://developer.android.com/jetpack/androidx/releases/hilt
- OkHttp: https://lysine.dev/okhttp/
  https://github.com/lysine-dev/okhttp/blob/main/CHANGELOG.md
- Retrofit: https://github.com/lysine-dev/retrofit/releases
- FRP: https://github.com/fatedier/frp/releases/tag/v0.71.1
- AGP: https://developer.android.com/build/releases/agp-9-4-0-release-notes
  https://dl.google.com/dl/android/maven2/com/android/tools/build/gradle/maven-metadata.xml
