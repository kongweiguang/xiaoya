/**
 * Copyright(c) Live2D Inc. All rights reserved.
 *
 * Use of this source code is governed by the Live2D Open Software license
 * that can be found at https://www.live2d.com/eula/live2d-open-software-license-agreement_en.html.
 */
export class CubismMotionSyncEngineVersion {
    constructor(rawVersion) {
        this._versionNumber = rawVersion;
        this._major = (this._versionNumber & 0xff000000) >> 24;
        this._minor = (this._versionNumber & 0x00ff0000) >> 16;
        this._patch = this._versionNumber & 0x0000ffff;
    }
    getMajor() {
        return this._major;
    }
    getMinor() {
        return this._minor;
    }
    getPatch() {
        return this._patch;
    }
    toString() {
        return (this._major +
            '.' +
            this._minor +
            '.' +
            this._patch +
            '(' +
            this._versionNumber +
            ')');
    }
}
// Namespace definition for compatibility.
import * as $ from './cubismmotionsyncengineversion';
// eslint-disable-next-line @typescript-eslint/no-namespace
export var Live2DCubismMotionSyncFramework;
(function (Live2DCubismMotionSyncFramework) {
    Live2DCubismMotionSyncFramework.CubismMotionSyncEngineVersion = $.CubismMotionSyncEngineVersion;
})(Live2DCubismMotionSyncFramework || (Live2DCubismMotionSyncFramework = {}));
