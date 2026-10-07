/**
 * Copyright(c) Live2D Inc. All rights reserved.
 *
 * Use of this source code is governed by the Live2D Open Software license
 * that can be found at https://www.live2d.com/eula/live2d-open-software-license-agreement_en.html.
 */
// Engine側に渡すBitDepth
export const DefaultAudioBitDepth = 32;
export class ICubismMotionSyncEngine {
    constructor(engineHandle, type, name, version) {
        this._engineHandle = engineHandle;
        this._type = type;
        this._name = name;
        this._version = version;
    }
    getType() {
        return this._type;
    }
    getName() {
        return this._name;
    }
    getVersion() {
        return this._version;
    }
    getEngineHandle() {
        return this._engineHandle;
    }
    getProcessors() {
        return this._processors;
    }
    isClosed() {
        return this.getEngineHandle() == null;
    }
    releaseAllProcessor() {
        if (this.isClosed()) {
            return;
        }
        for (let index = 0; index < this._processors.getSize(); index++) {
            this._processors.at(index).Close();
        }
    }
    close(isForce) {
        if (this.isClosed()) {
            return;
        }
        if (0 < this._processors.getSize()) {
            if (isForce) {
                this.releaseAllProcessor();
            }
            else {
                return;
            }
        }
        this.getEngineHandle().disposeEngine();
        this._engineHandle = void 0;
        this._engineHandle = null;
        CubismMotionSyncEngineController.deleteAssociation(this);
    }
    DeleteAssociation(processor) {
        for (let index = 0; index < this._processors.getSize(); index++) {
            if (this._processors.at(index) == processor) {
                this._processors.at(index).Close();
                this._processors.remove(index);
                break;
            }
        }
    }
}
// Namespace definition for compatibility.
import * as $ from './icubismmotionsyncengine';
import { CubismMotionSyncEngineController } from './cubismmotionsyncenginecontroller';
// eslint-disable-next-line @typescript-eslint/no-namespace
export var Live2DCubismMotionSyncFramework;
(function (Live2DCubismMotionSyncFramework) {
    Live2DCubismMotionSyncFramework.ICubismMotionSyncEngine = $.ICubismMotionSyncEngine;
})(Live2DCubismMotionSyncFramework || (Live2DCubismMotionSyncFramework = {}));
