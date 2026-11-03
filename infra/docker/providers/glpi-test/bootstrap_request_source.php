<?php

declare(strict_types=1);

use Glpi\Application\ResourcesChecker;
use Glpi\Kernel\Kernel;

/**
 * Restrict this development/test bootstrap to the expected request source.
 */
function required_request_source_name(): string
{
    $name = getenv('GLPI_TEST_REQUEST_SOURCE_NAME');
    if ($name === false || trim($name) !== 'MEVO') {
        throw new RuntimeException(
            'GLPI_TEST_REQUEST_SOURCE_NAME must identify the MEVO test source'
        );
    }

    return trim($name);
}

$stage = 'read_environment';

try {
    $name = required_request_source_name();

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

    $stage = 'ensure_request_source';
    $requestType = new RequestType();
    if (!$requestType->getFromDBByCrit(['name' => $name])) {
        $requestTypeId = $requestType->add([
            'name' => $name,
            'is_active' => 1,
            'is_ticketheader' => 1,
        ]);
        if ($requestTypeId === false || $requestTypeId === 0) {
            throw new RuntimeException('The GLPI test request source could not be created');
        }

        $requestType = new RequestType();
        if (!$requestType->getFromDB((int) $requestTypeId)) {
            throw new RuntimeException('The new GLPI test request source could not be loaded');
        }
    }

    if (
        (int) ($requestType->fields['is_active'] ?? 0) !== 1
        || (int) ($requestType->fields['is_ticketheader'] ?? 0) !== 1
    ) {
        $updated = $requestType->update([
            'id' => (int) $requestType->getID(),
            'is_active' => 1,
            'is_ticketheader' => 1,
        ]);
        if (!$updated) {
            throw new RuntimeException('The GLPI test request source could not be activated');
        }

        $requestType = new RequestType();
        if (!$requestType->getFromDBByCrit(['name' => $name])) {
            throw new RuntimeException('The updated GLPI test request source could not be loaded');
        }
    }

    if (
        (string) ($requestType->fields['name'] ?? '') !== $name
        || (int) ($requestType->fields['is_active'] ?? 0) !== 1
        || (int) ($requestType->fields['is_ticketheader'] ?? 0) !== 1
    ) {
        throw new RuntimeException('The GLPI test request source has an invalid contract');
    }

    $stage = 'write_result';
    $result = [
        'id' => (int) $requestType->getID(),
        'name' => $name,
        'is_active' => true,
        'is_ticketheader' => true,
    ];

    fwrite(
        STDOUT,
        json_encode($result, JSON_THROW_ON_ERROR | JSON_UNESCAPED_SLASHES) . PHP_EOL
    );
} catch (Throwable $exception) {
    fwrite(
        STDERR,
        sprintf(
            "GLPI request source test bootstrap failed at %s (%s)%s",
            $stage,
            $exception::class,
            PHP_EOL
        )
    );
    exit(1);
}
