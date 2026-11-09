import type { PluginData } from "../settings";
import { tr, type UiLanguage } from "../i18n";
import { profileNoteName } from "./profile-note";
import { userFolder } from "./note/user-folder";
export interface ProfilePathPort { exists(path: string): boolean; rename(from: string, to: string): Promise<void>; save(): Promise<void> }
/** A note's persisted identity owns its path. The language only chooses its initial name. */
export async function resolveProfilePath(data: Pick<PluginData, "settings" | "profilePaths" | "profileBases">, language: UiLanguage, port: ProfilePathPort): Promise<string> {
  const identity = JSON.stringify([data.settings.serverUrl, data.settings.userId]);
  const paths = data.profilePaths ??= {};
  if (paths[identity]) return paths[identity];
  const folder = userFolder(data.settings);
  const destination = `${folder}/${profileNoteName(language)}`;
  const legacy = `${folder}/我.md`;
  if (port.exists(legacy)) {
    if (port.exists(destination)) throw new Error(tr("runtime.profileRenameConflict", { path: destination }));
    await port.rename(legacy, destination);
    const version = data.profileBases[legacy];
    if (version !== undefined) { data.profileBases[destination] = version; delete data.profileBases[legacy]; }
  }
  paths[identity] = destination;
  await port.save();
  return destination;
}
