type JsonRecord = Record<string, unknown>;

function decodeBase64Url(value: string): ArrayBuffer {
  const normalized = value.replace(/-/g, "+").replace(/_/g, "/");
  const binary = atob(normalized + "=".repeat((4 - (normalized.length % 4)) % 4));
  const bytes = new Uint8Array(binary.length);
  for (let index = 0; index < binary.length; index += 1)
    bytes[index] = binary.charCodeAt(index);
  return bytes.buffer;
}

function encodeBase64Url(value: ArrayBuffer): string {
  const bytes = new Uint8Array(value);
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/g, "");
}

function creationOptions(value: JsonRecord): PublicKeyCredentialCreationOptions {
  const user = value.user as JsonRecord;
  const exclude = (value.excludeCredentials ?? []) as JsonRecord[];
  return {
    ...(value as unknown as PublicKeyCredentialCreationOptions),
    challenge: decodeBase64Url(String(value.challenge)),
    user: {
      ...user,
      id: decodeBase64Url(String(user.id)),
    } as PublicKeyCredentialUserEntity,
    excludeCredentials: exclude.map((item) => ({
      ...item,
      id: decodeBase64Url(String(item.id)),
    })) as PublicKeyCredentialDescriptor[],
  };
}

function requestOptions(value: JsonRecord): PublicKeyCredentialRequestOptions {
  const allow = (value.allowCredentials ?? []) as JsonRecord[];
  return {
    ...(value as unknown as PublicKeyCredentialRequestOptions),
    challenge: decodeBase64Url(String(value.challenge)),
    allowCredentials: allow.map((item) => ({
      ...item,
      id: decodeBase64Url(String(item.id)),
    })) as PublicKeyCredentialDescriptor[],
  };
}

function credentialPayload(credential: PublicKeyCredential): JsonRecord {
  const response = credential.response;
  const common: JsonRecord = {
    id: credential.id,
    rawId: encodeBase64Url(credential.rawId),
    type: credential.type,
    authenticatorAttachment: credential.authenticatorAttachment,
    clientExtensionResults: credential.getClientExtensionResults(),
  };
  if (response instanceof AuthenticatorAttestationResponse) {
    return {
      ...common,
      response: {
        clientDataJSON: encodeBase64Url(response.clientDataJSON),
        attestationObject: encodeBase64Url(response.attestationObject),
        transports: response.getTransports?.() ?? [],
      },
    };
  }
  if (response instanceof AuthenticatorAssertionResponse) {
    return {
      ...common,
      response: {
        clientDataJSON: encodeBase64Url(response.clientDataJSON),
        authenticatorData: encodeBase64Url(response.authenticatorData),
        signature: encodeBase64Url(response.signature),
        userHandle: response.userHandle
          ? encodeBase64Url(response.userHandle)
          : null,
      },
    };
  }
  throw Error("Unsupported WebAuthn credential response");
}

async function postJson(path: string, body: JsonRecord): Promise<JsonRecord> {
  const response = await fetch(path, {
    method: "POST",
    credentials: "same-origin",
    cache: "no-store",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!response.ok) throw Error("WebAuthn verification was rejected");
  return (await response.json()) as JsonRecord;
}

export async function completeOperatorMfa(
  loginResult: JsonRecord,
): Promise<void> {
  if (!navigator.credentials || !window.PublicKeyCredential)
    throw Error("This browser does not support WebAuthn security keys");
  let next = loginResult;
  if (next.mfa_enrollment_required === true) {
    const options = creationOptions(next.options as JsonRecord);
    const created = await navigator.credentials.create({ publicKey: options });
    if (!(created instanceof PublicKeyCredential))
      throw Error("No WebAuthn authenticator response was received");
    next = await postJson("/operator/v1/mfa/register", {
      credential: credentialPayload(created),
    });
  }
  if (next.mfa_required !== true)
    throw Error("The gateway did not request a WebAuthn second factor");
  const options = requestOptions(next.options as JsonRecord);
  const assertion = await navigator.credentials.get({ publicKey: options });
  if (!(assertion instanceof PublicKeyCredential))
    throw Error("No WebAuthn assertion was received");
  await postJson("/operator/v1/mfa/verify", {
    challenge: String((next.options as JsonRecord).challenge),
    assertion: credentialPayload(assertion),
  });
}

export async function performWebAuthnAssertion(
  optionsValue: JsonRecord,
): Promise<JsonRecord> {
  if (!navigator.credentials || !window.PublicKeyCredential)
    throw Error("This browser does not support WebAuthn security keys");
  const options = requestOptions(optionsValue);
  const assertion = await navigator.credentials.get({ publicKey: options });
  if (!(assertion instanceof PublicKeyCredential))
    throw Error("No WebAuthn assertion was received");
  return credentialPayload(assertion);
}
