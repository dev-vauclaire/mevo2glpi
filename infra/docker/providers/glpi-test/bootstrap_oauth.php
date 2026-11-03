<?php

declare(strict_types=1);

use Glpi\Application\ResourcesChecker;
use Glpi\Kernel\Kernel;
use Glpi\OAuth\Server;

/**
 * Read a test-only value without ever copying its content into an error message.
 */
function required_test_environment(string $name): string
{
    $value = getenv($name);
    if ($value === false || trim($value) === '') {
        throw new RuntimeException("Missing required test environment variable: {$name}");
    }

    return $value;
}

/**
 * Restrict persistent names to the namespace reserved for disposable tests.
 */
function assert_test_only_name(string $value, string $environmentName): void
{
    if (preg_match('/^glpi-test-[a-z0-9-]+$/', $value) !== 1) {
        throw new RuntimeException(
            "The value of {$environmentName} must use the glpi-test- namespace"
        );
    }
}

/**
 * Validate one configured OAuth identifier without exposing its value.
 */
function assert_test_oauth_identifier(string $value): void
{
    if (
        strlen($value) < 16
        || strlen($value) > 128
        || preg_match('/^[A-Za-z0-9._-]+$/', $value) !== 1
    ) {
        throw new RuntimeException('GLPI_TEST_CLIENT_ID has an invalid format');
    }
}

/**
 * Keep synthetic secrets usable without accepting accidental empty values.
 */
function assert_test_oauth_secret(string $value): void
{
    if (strlen($value) < 32 || strlen($value) > 256) {
        throw new RuntimeException(
            'GLPI_TEST_CLIENT_SECRET must contain between 32 and 256 characters'
        );
    }
}

$stage = 'read_environment';

try {
    $username = required_test_environment('GLPI_TEST_USERNAME');
    $password = required_test_environment('GLPI_TEST_PASSWORD');
    $clientName = required_test_environment('GLPI_TEST_OAUTH_CLIENT_NAME');
    $clientIdentifier = required_test_environment('GLPI_TEST_CLIENT_ID');
    $clientSecret = required_test_environment('GLPI_TEST_CLIENT_SECRET');
    $timezone = required_test_environment('GLPI_TEST_TIMEZONE');

    assert_test_only_name($username, 'GLPI_TEST_USERNAME');
    assert_test_only_name($clientName, 'GLPI_TEST_OAUTH_CLIENT_NAME');
    assert_test_oauth_identifier($clientIdentifier);
    assert_test_oauth_secret($clientSecret);
    if ($timezone !== 'Europe/Paris') {
        throw new RuntimeException('GLPI_TEST_TIMEZONE must be Europe/Paris');
    }

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

    $stage = 'ensure_test_user';
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
            'timezone' => $timezone,
        ]);
        if ($userId === false || $userId === 0) {
            throw new RuntimeException('The GLPI test user could not be created');
        }

        $user = new User();
        if (!$user->getFromDB((int) $userId)) {
            throw new RuntimeException('The new GLPI test user could not be loaded');
        }
    }

    if ((string) ($user->fields['timezone'] ?? '') !== $timezone) {
        $updated = $user->update([
            'id' => (int) $user->getID(),
            'timezone' => $timezone,
        ]);
        if (!$updated) {
            throw new RuntimeException('The GLPI test user timezone could not be set');
        }

        $user = new User();
        if (!$user->getFromDBByCrit($userCriteria)) {
            throw new RuntimeException('The updated GLPI test user could not be loaded');
        }
    }

    if (
        (int) ($user->fields['is_active'] ?? 0) !== 1
        || (string) ($user->fields['timezone'] ?? '') !== $timezone
        || !Auth::checkPassword($password, (string) ($user->fields['password'] ?? ''))
    ) {
        throw new RuntimeException('The GLPI test user is not usable');
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
            throw new RuntimeException('The GLPI OAuth test client could not be created');
        }

        $oauthClient = new OAuthClient();
        if (!$oauthClient->getFromDB((int) $oauthClientId)) {
            throw new RuntimeException('The new GLPI OAuth test client could not be loaded');
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
            'The existing GLPI OAuth test credentials differ from the configured contract'
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
            throw new RuntimeException('The GLPI OAuth test client could not be reconciled');
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
        throw new RuntimeException('The GLPI OAuth test client is not usable');
    }

    $stage = 'write_result';
    $result = [
        'username' => $username,
        'password' => $password,
        'user_id' => (int) $user->getID(),
        'client_id' => $clientIdentifier,
        'client_secret' => $clientSecret,
        'oauth_client_id' => (int) $oauthClient->getID(),
        'grant_type' => 'password',
        'scope' => 'api',
        'timezone' => $timezone,
    ];

    fwrite(
        STDOUT,
        json_encode($result, JSON_THROW_ON_ERROR | JSON_UNESCAPED_SLASHES) . PHP_EOL
    );
} catch (Throwable $exception) {
    fwrite(
        STDERR,
        sprintf(
            "GLPI OAuth test bootstrap failed at %s (%s)%s",
            $stage,
            $exception::class,
            PHP_EOL
        )
    );
    exit(1);
}
