export function config(service: any) {
  return {
    jwksP1: service.getOrThrow('KEYCLOAK_JWKS_URI_DP_P1'),
    jwksP2: service.getOrThrow('KEYCLOAK_JWKS_URI_DP_P2'),
    identity: service.getOrThrow('IDENTITY_TRUST_BASE_URL'),
    notification: service.getOrThrow('NOTIFICATION_BASE_URL'),
  };
}
