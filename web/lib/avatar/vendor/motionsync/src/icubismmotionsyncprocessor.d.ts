/**
 * Copyright(c) Live2D Inc. All rights reserved.
 *
 * Use of this source code is governed by the Live2D Open Software license
 * that can be found at https://www.live2d.com/eula/live2d-open-software-license-agreement_en.html.
 */
import { csmVector } from '@framework/type/csmvector';
import { CubismMotionSyncEngineAnalysisResult } from './cubismmotionsyncengineanalysisresult';
import { CubismMotionSyncEngineMappingInfo } from './cubismmotionsyncenginemappinginfo';
import { EngineType, MotionSyncContext } from './cubismmotionsyncutil';
import { ICubismMotionSyncEngine } from './icubismmotionsyncengine';
export declare abstract class ICubismMotionSyncProcessor {
    /**
     * createAnalysisResult
     */
    createAnalysisResult(): CubismMotionSyncEngineAnalysisResult;
    /**
     * isClosed
     */
    isClosed(): boolean;
    Close(): void;
    getContextHandle(): MotionSyncContext;
    getEngine(): ICubismMotionSyncEngine;
    getType(): EngineType;
    getRequireSampleCount(): number;
    constructor(engine: ICubismMotionSyncEngine, contextHandle: MotionSyncContext, mappingList: csmVector<CubismMotionSyncEngineMappingInfo>);
    private _engine;
    private _contextHandle;
    private _mappingInfoArray;
}
import * as $ from './icubismmotionsyncprocessor';
export declare namespace Live2DCubismMotionSyncFramework {
    const ICubismMotionSyncProcessor: typeof $.ICubismMotionSyncProcessor;
    type ICubismMotionSyncProcessor = $.ICubismMotionSyncProcessor;
}
