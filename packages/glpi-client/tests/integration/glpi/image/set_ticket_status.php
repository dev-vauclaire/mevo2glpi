<?php

declare(strict_types=1);

use Glpi\Application\ResourcesChecker;
use Glpi\Kernel\Kernel;

$stage = 'read_environment';

try {
    $ticketIdRaw = getenv('GLPI_TEST_TICKET_ID');
    $targetStatusRaw = getenv('GLPI_TEST_TICKET_STATUS');
    $userIdRaw = getenv('GLPI_TEST_USER_ID');
    $username = getenv('GLPI_TEST_USERNAME');

    if (
        $ticketIdRaw === false
        || filter_var(
            $ticketIdRaw,
            FILTER_VALIDATE_INT,
            ['options' => ['min_range' => 1]]
        ) === false
        || $userIdRaw === false
        || filter_var(
            $userIdRaw,
            FILTER_VALIDATE_INT,
            ['options' => ['min_range' => 1]]
        ) === false
        || $targetStatusRaw === false
        || filter_var($targetStatusRaw, FILTER_VALIDATE_INT) === false
        || $username === false
        || preg_match('/^glpi-test-[a-z0-9-]+$/', $username) !== 1
    ) {
        throw new RuntimeException('Invalid test transition environment');
    }

    $ticketId = (int) $ticketIdRaw;
    $userId = (int) $userIdRaw;
    $targetStatus = (int) $targetStatusRaw;

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

    $stage = 'validate_target';
    if (!in_array($targetStatus, [Ticket::SOLVED, Ticket::CLOSED], true)) {
        throw new RuntimeException('Unsupported test target status');
    }

    $stage = 'validate_user';
    $user = new User();
    if (
        !$user->getFromDB($userId)
        || ($user->fields['name'] ?? null) !== $username
    ) {
        throw new RuntimeException('Inconsistent GLPI test user identity');
    }

    $stage = 'load_ticket';
    $ticket = new Ticket();
    if (!$ticket->getFromDB($ticketId)) {
        throw new RuntimeException('Test ticket not found');
    }
    if ((int) ($ticket->fields['entities_id'] ?? -1) !== 0) {
        throw new RuntimeException('Test ticket is outside the root entity');
    }

    $stage = 'prepare_session';
    // These minimal rights exist only in this CLI process. No profile or
    // authorization is persisted in the disposable GLPI database.
    $_SESSION['glpi_currenttime'] = date('Y-m-d H:i:s');
    $_SESSION['glpiID'] = $userId;
    $_SESSION['glpiname'] = $username;
    $_SESSION['glpiactive_entity'] = 0;
    $_SESSION['glpiactiveentities'] = [0];
    $_SESSION['glpiactiveprofile'] = [
        'ticket' => UPDATE,
        Ticket::STATUS_MATRIX_FIELD => [
            Ticket::INCOMING => [Ticket::SOLVED => 1],
            Ticket::SOLVED => [Ticket::CLOSED => 1],
        ],
    ];

    $stage = 'validate_transition';
    $currentStatus = (int) $ticket->fields['status'];
    if (!Ticket::isAllowedStatus($currentStatus, $targetStatus)) {
        throw new RuntimeException('Unsupported test ticket transition');
    }

    $stage = 'update_ticket';
    if (!$ticket->update(['id' => $ticketId, 'status' => $targetStatus])) {
        throw new RuntimeException('Test ticket transition failed');
    }

    $stage = 'verify_transition';
    if (
        !$ticket->getFromDB($ticketId)
        || (int) $ticket->fields['status'] !== $targetStatus
    ) {
        throw new RuntimeException('Test ticket transition was not persisted');
    }

    $stage = 'write_result';
    fwrite(
        STDOUT,
        json_encode(
            ['ticket_id' => $ticketId, 'status' => $targetStatus],
            JSON_THROW_ON_ERROR
        ) . PHP_EOL
    );
} catch (Throwable $exception) {
    fwrite(
        STDERR,
        sprintf(
            "GLPI test ticket transition failed at %s (%s)%s",
            $stage,
            $exception::class,
            PHP_EOL
        )
    );
    exit(1);
}
