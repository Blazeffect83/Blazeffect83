-- The Minecraft player check was removed: vitals no longer record players online.
ALTER TABLE vitals DROP COLUMN players;
