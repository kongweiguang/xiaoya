/**
 * Copyright(c) Live2D Inc. All rights reserved.
 *
 * Use of this source code is governed by the Live2D Open Software license
 * that can be found at https://www.live2d.com/eula/live2d-open-software-license-agreement_en.html.
 */
import { csmVector } from '@framework/type/csmvector';
import { csmString } from '@framework/type/csmstring';
import { ICubismMotionSyncProcessor } from './icubismmotionsyncprocessor';
import { CubismMotionSyncEngineLib } from './cubismmotionsyncenginelib';
import { EngineType } from './cubismmotionsyncutil';
import { CubismMotionSyncEngineVersion } from './cubismmotionsyncengineversion';
export declare const DefaultAudioBitDepth: number;
export declare abstract class ICubismMotionSyncEngine {
    constructor(engineHandle: CubismMotionSyncEngineLib, type: EngineType, name: csmString, version: CubismMotionSyncEngineVersion);
    getType(): EngineType;
    getName(): csmString;
    getVersion(): CubismMotionSyncEngineVersion;
    getEngineHandle(): CubismMotionSyncEngineLib;
    getProcessors(): csmVector<ICubismMotionSyncProcessor>;
    isClosed(): boolean;
    releaseAllProcessor(): void;
    close(isForce: boolean): void;
    DeleteAssociation(processor: ICubismMotionSyncProcessor): void;
    protected _processors: csmVector<ICubismMotionSyncProcessor>;
    private _engineHandle;
    private _type;
    private _name;
    private _version;
}
import * as $ from './icubismmotionsyncengine';
export declare namespace Live2DCubismMotionSyncFramework {
    const ICubismMotionSyncEngine: typeof $.ICubismMotionSyncEngine;
    type ICubismMotionSyncEngine = $.ICubismMotionSyncEngine;
}
