import type { PluginData } from "../settings";
import type { UiLanguage } from "../i18n";
import { profileNoteName } from "./profile-note";
import { userFolder } from "./note/user-folder";
export interface ProfilePathPort { exists(path: string): boolean; rename(from: string, to: string): Promise<void>; save(): Promise<void> }
/** A note's persisted identity owns its path. The language only chooses its initial name. */
export async function resolveProfilePath(data: Pick<PluginData, "settings" | "profilePaths" | "profileBases">, language: UiLanguage, port: ProfilePathPort): Promise<string> {
  const identity = JSON.stringify([data.settings.serverUrl, data.settings.userId]);
  const paths = data.profilePaths ??= {};
  if (paths[identity]) return paths[identity];
  const destination = `${userFolder(data.settings)}/${profileNoteName(language)}`;
  paths[identity] = destination;
  await port.save();
  return destination;
}
