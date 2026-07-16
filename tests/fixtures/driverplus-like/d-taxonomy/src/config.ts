export function load(configService: any) {
  return {
    database: configService.getOrThrow('DATABASE_URL'),
    redisHost: configService.getOrThrow('REDIS_HOST'),
    redisPort: configService.getOrThrow('REDIS_PORT'),
    brokers: configService.getOrThrow('KAFKA_BROKERS'),
    audience: configService.getOrThrow('KEYCLOAK_AUDIENCE'),
    role: configService.getOrThrow('KEYCLOAK_REQUIRED_ROLE'),
    resourceRoles: configService.get('TOKEN')?.resource_access,
  };
}
