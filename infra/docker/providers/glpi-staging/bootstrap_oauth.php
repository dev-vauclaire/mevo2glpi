<?php

declare(strict_types=1);

use Glpi\Application\ResourcesChecker;
use Glpi\Kernel\Kernel;
use Glpi\OAuth\Server;

/**
 * Read a required value without ever copying its content into diagnostics.
 */
function required_staging_environment(string $name): string
{
    $value = getenv($name);
    if ($value === false || trim($value) === '') {
        throw new RuntimeException("Missing required staging variable: {$name}");
    }
    if (str_contains(strtoupper($value), 'CHANGE_ME')) {
        throw new RuntimeException(
            "The staging placeholder for {$name} must be replaced"
        );
    }

    return $value;
}

/**
 * Prevent this persistent bootstrap from targeting a production identity.
 */
function assert_staging_name(string $value, string $environmentName): void
{
    if (preg_match('/^glpi-staging-[a-z0-9-]+$/', $value) !== 1) {
        throw new RuntimeException(
            "The value of {$environmentName} must use the glpi-staging- namespace"
        );
    }
}

/**
 * Validate identifiers without exposing them in an error message.
 */
function assert_oauth_identifier(string $value): void
{
    if (
        strlen($value) < 16
        || strlen($value) > 128
        || preg_match('/^[A-Za-z0-9._-]+$/', $value) !== 1
    ) {
        throw new RuntimeException('GLPI_CLIENT_ID has an invalid format');
    }
}

/**
 * Keep credentials strong enough for a remotely reachable staging service.
 */
function assert_secret_length(string $value, string $environmentName): void
{
    if (strlen($value) < 32 || strlen($value) > 256) {
        throw new RuntimeException(
            "{$environmentName} must contain between 32 and 256 characters"
        );
    }
}

/**
 * Remove the readiness marker before reconciling persistent GLPI state.
 */
function clear_staging_ready_marker(string $path): void
{
    if ((file_exists($path) || is_link($path)) && !unlink($path)) {
        throw new RuntimeException('The GLPI staging readiness marker could not be removed');
    }
}

/**
 * Publish readiness only after every business invariant has been verified.
 */
function write_staging_ready_marker(string $path): void
{
    $directory = dirname($path);
    $temporaryPath = tempnam($directory, '.gta-staging-bootstrap-ready.');
    if ($temporaryPath === false) {
        throw new RuntimeException('A temporary GLPI readiness marker could not be created');
    }

    try {
        $content = "11.0.7\n";
        if (file_put_contents($temporaryPath, $content, LOCK_EX) !== strlen($content)) {
            throw new RuntimeException('The GLPI staging readiness marker could not be written');
        }
        if (!chmod($temporaryPath, 0640)) {
            throw new RuntimeException(
                'The GLPI staging readiness marker permissions could not be applied'
            );
        }
        if (!rename($temporaryPath, $path)) {
            throw new RuntimeException('The GLPI staging readiness marker could not be published');
        }
    } finally {
        if (file_exists($temporaryPath) || is_link($temporaryPath)) {
            unlink($temporaryPath);
        }
    }
}

$stage = 'read_environment';
$readyMarkerPath = '/var/glpi/.gta-staging-bootstrap-ready';

try {
    $username = required_staging_environment('GLPI_USERNAME');
    $password = required_staging_environment('GLPI_PASSWORD');
    $adminUsername = required_staging_environment('GLPI_ADMIN_USERNAME');
    $adminPassword = required_staging_environment('GLPI_ADMIN_PASSWORD');
    $clientName = required_staging_environment('GLPI_OAUTH_CLIENT_NAME');
    $clientIdentifier = required_staging_environment('GLPI_CLIENT_ID');
    $clientSecret = required_staging_environment('GLPI_CLIENT_SECRET');

    assert_staging_name($username, 'GLPI_USERNAME');
    assert_staging_name($adminUsername, 'GLPI_ADMIN_USERNAME');
    assert_staging_name($clientName, 'GLPI_OAUTH_CLIENT_NAME');
    if ($adminUsername === $username) {
        throw new RuntimeException('GLPI staging admin and service users must differ');
    }
    assert_oauth_identifier($clientIdentifier);
    assert_secret_length($password, 'GLPI_PASSWORD');
    assert_secret_length($adminPassword, 'GLPI_ADMIN_PASSWORD');
    assert_secret_length($clientSecret, 'GLPI_CLIENT_SECRET');

    $stage = 'boot_glpi';
    $glpiRoot = '/var/www/glpi';
    chdir($glpiRoot);
    require_once $glpiRoot . '/src/Glpi/Application/ResourcesChecker.php';
    (new ResourcesChecker($glpiRoot))->checkResources();
    require_once $glpiRoot . '/vendor/autoload.php';

    $kernel = new Kernel();
    $kernel->boot();

    if (!DBConnection::isDbAvailable()) {
        throw new RuntimeException('GLPI database is not available');
    }

    $stage = 'clear_ready_marker';
    clear_staging_ready_marker($readyMarkerPath);

    $stage = 'enable_api_v2';
    Config::setConfigurationValues('core', ['enable_hlapi' => 1]);
    if ((int) Config::getConfigurationValue('core', 'enable_hlapi') !== 1) {
        throw new RuntimeException('GLPI API V2 could not be enabled');
    }

    $stage = 'ensure_oauth_keys';
    if (!Server::checkKeys()) {
        Server::generateKeys(false);
    }
    if (!Server::checkKeys()) {
        throw new RuntimeException('GLPI OAuth keys are unavailable');
    }

    $stage = 'ensure_staging_administrator';
    $admin = new User();
    if (!$admin->getFromDBByCrit(['name' => $adminUsername])) {
        $adminId = $admin->add([
            'name' => $adminUsername,
            'password' => $adminPassword,
            'password2' => $adminPassword,
            'is_active' => 1,
        ]);
        if ($adminId === false || $adminId === 0) {
            throw new RuntimeException('The GLPI staging administrator could not be created');
        }

        $admin = new User();
        if (!$admin->getFromDB((int) $adminId)) {
            throw new RuntimeException('The new GLPI staging administrator could not be loaded');
        }
    }

    if (!Auth::checkPassword(
            $adminPassword,
            (string) ($admin->fields['password'] ?? '')
        )) {
        throw new RuntimeException(
            'The existing GLPI staging administrator credentials differ; rotate them explicitly'
        );
    }
    if ((int) ($admin->fields['is_active'] ?? 0) !== 1) {
        $updated = $admin->update([
            'id' => (int) $admin->getID(),
            'is_active' => 1,
        ]);
        if (!$updated || !$admin->getFromDB((int) $admin->getID())) {
            throw new RuntimeException('The GLPI staging administrator could not be enabled');
        }
    }
    if (
        (int) ($admin->fields['is_active'] ?? 0) !== 1
        || !Auth::checkPassword(
            $adminPassword,
            (string) ($admin->fields['password'] ?? '')
        )
    ) {
        throw new RuntimeException('The GLPI staging administrator is not usable');
    }

    $stage = 'ensure_super_admin_profile';
    $superAdminProfile = new Profile();
    if (!$superAdminProfile->getFromDBByCrit(['name' => 'Super-Admin'])) {
        throw new RuntimeException('The GLPI Super-Admin profile is unavailable');
    }
    $profileAuthorizationCriteria = [
        'users_id' => (int) $admin->getID(),
        'profiles_id' => (int) $superAdminProfile->getID(),
        'entities_id' => 0,
    ];
    $profileAuthorization = new Profile_User();
    if (!$profileAuthorization->getFromDBByCrit($profileAuthorizationCriteria)) {
        $authorizationId = $profileAuthorization->add([
            ...$profileAuthorizationCriteria,
            'is_recursive' => 1,
            'is_dynamic' => 0,
        ]);
        if ($authorizationId === false || $authorizationId === 0) {
            throw new RuntimeException(
                'The GLPI staging administrator profile could not be assigned'
            );
        }

        $profileAuthorization = new Profile_User();
        if (!$profileAuthorization->getFromDB((int) $authorizationId)) {
            throw new RuntimeException(
                'The new GLPI staging administrator profile could not be loaded'
            );
        }
    }
    if ((int) ($profileAuthorization->fields['is_recursive'] ?? 0) !== 1) {
        $updated = $profileAuthorization->update([
            'id' => (int) $profileAuthorization->getID(),
            'is_recursive' => 1,
        ]);
        if (
            !$updated
            || !$profileAuthorization->getFromDB(
                (int) $profileAuthorization->getID()
            )
        ) {
            throw new RuntimeException(
                'The GLPI staging administrator profile could not be reconciled'
            );
        }
    }
    if (
        (int) ($profileAuthorization->fields['users_id'] ?? 0)
            !== (int) $admin->getID()
        || (int) ($profileAuthorization->fields['profiles_id'] ?? 0)
            !== (int) $superAdminProfile->getID()
        || (int) ($profileAuthorization->fields['entities_id'] ?? -1) !== 0
        || (int) ($profileAuthorization->fields['is_recursive'] ?? 0) !== 1
    ) {
        throw new RuntimeException(
            'The GLPI staging administrator profile is not usable'
        );
    }

    $stage = 'disable_default_users';
    foreach (['glpi', 'tech', 'normal', 'post-only'] as $defaultUsername) {
        $defaultUser = new User();
        if (
            $defaultUser->getFromDBByCrit(['name' => $defaultUsername])
            && (int) ($defaultUser->fields['is_active'] ?? 0) !== 0
            && !$defaultUser->update([
                'id' => (int) $defaultUser->getID(),
                'is_active' => 0,
            ])
        ) {
            throw new RuntimeException('A default GLPI account could not be disabled');
        }
    }

    $stage = 'ensure_staging_user';
    $user = new User();
    $userCriteria = [
        'name' => $username,
        'authtype' => Auth::DB_GLPI,
        'auths_id' => 0,
    ];

    if (!$user->getFromDBByCrit($userCriteria)) {
        $userId = $user->add([
            'name' => $username,
            'password' => $password,
            'password2' => $password,
            'is_active' => 1,
        ]);
        if ($userId === false || $userId === 0) {
            throw new RuntimeException('The GLPI staging user could not be created');
        }

        $user = new User();
        if (!$user->getFromDB((int) $userId)) {
            throw new RuntimeException('The new GLPI staging user could not be loaded');
        }
    }

    if (!Auth::checkPassword(
        $password,
        (string) ($user->fields['password'] ?? '')
    )) {
        throw new RuntimeException(
            'The existing GLPI staging service credentials differ; rotate them explicitly'
        );
    }
    if ((int) ($user->fields['is_active'] ?? 0) !== 1) {
        $updated = $user->update([
            'id' => (int) $user->getID(),
            'is_active' => 1,
        ]);
        if (!$updated || !$user->getFromDB((int) $user->getID())) {
            throw new RuntimeException('The GLPI staging user could not be enabled');
        }
    }

    if (
        (int) ($user->fields['is_active'] ?? 0) !== 1
        || !Auth::checkPassword($password, (string) ($user->fields['password'] ?? ''))
    ) {
        throw new RuntimeException('The GLPI staging user is not usable');
    }

    $stage = 'ensure_oauth_client';
    $oauthClient = new OAuthClient();
    $oauthClientWasCreated = false;
    if (!$oauthClient->getFromDBByCrit(['name' => $clientName])) {
        $oauthClientId = $oauthClient->add([
            'name' => $clientName,
            'is_active' => 1,
            'is_confidential' => 1,
            'grants' => ['password'],
            'scopes' => ['api'],
        ]);
        if ($oauthClientId === false || $oauthClientId === 0) {
            throw new RuntimeException('The GLPI staging OAuth client could not be created');
        }

        $oauthClient = new OAuthClient();
        if (!$oauthClient->getFromDB((int) $oauthClientId)) {
            throw new RuntimeException('The new OAuth client could not be loaded');
        }
        $oauthClientWasCreated = true;
    }

    $storedClientSecret = (new GLPIKey())->decrypt(
        (string) ($oauthClient->fields['secret'] ?? '')
    );
    if (
        !$oauthClientWasCreated
        && (
            (string) ($oauthClient->fields['identifier'] ?? '') !== $clientIdentifier
            || !is_string($storedClientSecret)
            || !hash_equals($storedClientSecret, $clientSecret)
        )
    ) {
        throw new RuntimeException(
            'The existing GLPI OAuth credentials differ; rotate them explicitly'
        );
    }
    $clientNeedsUpdate = $oauthClientWasCreated || (
        (int) ($oauthClient->fields['is_active'] ?? 0) !== 1
        || (int) ($oauthClient->fields['is_confidential'] ?? 0) !== 1
        || ($oauthClient->fields['grants'] ?? []) !== ['password']
        || ($oauthClient->fields['scopes'] ?? []) !== ['api']
    );

    if ($clientNeedsUpdate) {
        $clientUpdate = [
            'id' => (int) $oauthClient->getID(),
            'is_active' => 1,
            'is_confidential' => 1,
            'grants' => ['password'],
            'scopes' => ['api'],
        ];
        if ($oauthClientWasCreated) {
            $clientUpdate['identifier'] = $clientIdentifier;
            $clientUpdate['secret'] = $clientSecret;
        }
        $updated = $oauthClient->update($clientUpdate);
        if (!$updated || !$oauthClient->getFromDB((int) $oauthClient->getID())) {
            throw new RuntimeException('The OAuth client could not be reconciled');
        }
    }

    $storedClientSecret = (new GLPIKey())->decrypt(
        (string) ($oauthClient->fields['secret'] ?? '')
    );
    if (
        (int) ($oauthClient->fields['is_active'] ?? 0) !== 1
        || (int) ($oauthClient->fields['is_confidential'] ?? 0) !== 1
        || ($oauthClient->fields['grants'] ?? []) !== ['password']
        || ($oauthClient->fields['scopes'] ?? []) !== ['api']
        || (string) ($oauthClient->fields['identifier'] ?? '') !== $clientIdentifier
        || !is_string($storedClientSecret)
        || !hash_equals($storedClientSecret, $clientSecret)
    ) {
        throw new RuntimeException('The GLPI staging OAuth client is not usable');
    }

    $stage = 'publish_readiness';
    write_staging_ready_marker($readyMarkerPath);

    $stage = 'write_result';
    fwrite(
        STDOUT,
        json_encode(
            [
                'status' => 'ready',
                'admin_user_id' => (int) $admin->getID(),
                'user_id' => (int) $user->getID(),
                'oauth_client_id' => (int) $oauthClient->getID(),
            ],
            JSON_THROW_ON_ERROR
        ) . PHP_EOL
    );
} catch (Throwable $exception) {
    fwrite(
        STDERR,
        sprintf(
            "GLPI staging bootstrap failed at %s (%s)%s",
            $stage,
            $exception::class,
            PHP_EOL
        )
    );
    exit(1);
}
