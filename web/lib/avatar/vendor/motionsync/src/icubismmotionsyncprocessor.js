/**
 * Copyright(c) Live2D Inc. All rights reserved.
 *
 * Use of this source code is governed by the Live2D Open Software license
 * that can be found at https://www.live2d.com/eula/live2d-open-software-license-agreement_en.html.
 */
import { CubismMotionSyncEngineAnalysisResult } from './cubismmotionsyncengineanalysisresult';
export class ICubismMotionSyncProcessor {
    /**
     * createAnalysisResult
     */
    createAnalysisResult() {
        if (this.isClosed() || this._mappingInfoArray.getSize() < 1) {
            return new CubismMotionSyncEngineAnalysisResult(0);
        }
        return new CubismMotionSyncEngineAnalysisResult(this._mappingInfoArray.at(0).getModelParameterValues().getSize());
    }
    /**
     * isClosed
     */
    isClosed() {
        return this._contextHandle == null;
    }
    Close() {
        // 解放済みなら何もしない。
        if (this.isClosed()) {
            return;
        }
        this._contextHandle.release();
        this._contextHandle = void 0;
        this._contextHandle = null;
        this._engine.DeleteAssociation(this);
    }
    getContextHandle() {
        return this._contextHandle;
    }
    getEngine() {
        return this._engine;
    }
    getType() {
        return this._engine.getType();
    }
    getRequireSampleCount() {
        var _a, _b;
        if (!((_a = this.getEngine()) === null || _a === void 0 ? void 0 : _a.getEngineHandle()) ||
            !((_b = this.getContextHandle()) === null || _b === void 0 ? void 0 : _b.getContext())) {
            return 0;
        }
        return this.getEngine()
            .getEngineHandle()
            .getRequireSampleCount(this.getContextHandle().getContext());
    }
    constructor(engine, contextHandle, mappingList) {
        this._engine = engine;
        this._contextHandle = contextHandle;
        this._mappingInfoArray = mappingList;
    }
}
// Namespace definition for compatibility.
import * as $ from './icubismmotionsyncprocessor';
// eslint-disable-next-line @typescript-eslint/no-namespace
export var Live2DCubismMotionSyncFramework;
(function (Live2DCubismMotionSyncFramework) {
    Live2DCubismMotionSyncFramework.ICubismMotionSyncProcessor = $.ICubismMotionSyncProcessor;
})(Live2DCubismMotionSyncFramework || (Live2DCubismMotionSyncFramework = {}));
